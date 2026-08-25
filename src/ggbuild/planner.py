# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Project-independent build-plan creation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import copy
import dataclasses
import importlib
import inspect
import pathlib
import re
import tomllib
from collections import defaultdict

import ggbuild.targets as _targets  # ruff: ignore[unused-import] - init order
from ggbuild import packages
from ggbuild.ci_protocol import (
    PLAN_FORMAT_VERSION,
    BuildPlan,
    PlanNode,
    digest_json,
    topological_layers,
    tree_digest,
    validate_plan,
)
from ggbuild.dist import get_project_version_key
from ggbuild.packages import repository
from ggbuild.project import BuildOptions, ProjectConfig, load_project
from ggbuild.targets.linux.dockerfile import docker_environment

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from poetry.core.packages import dependency as poetry_dependency

_NODE_SAFE = re.compile(r"[^a-z0-9]+")


@dataclasses.dataclass(frozen=True, slots=True)
class PlanOptions:
    targets: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    build: BuildOptions | None = None


def load_recipe(reference: str) -> type[packages.BundledPackage]:
    module_name, separator, class_name = reference.rpartition(":")
    if not separator or not module_name or not class_name:
        raise ValueError("recipe must use module:Class form")
    value = getattr(importlib.import_module(module_name), class_name)
    if not isinstance(value, type) or not issubclass(
        value, packages.BundledPackage
    ):
        raise TypeError(f"{reference} is not a bundled package recipe")
    return value


def registered_releases(
    recipe: type[packages.BundledPackage],
) -> tuple[packages.BundledPackage, ...]:
    releases = [
        package
        for package in repository.bundle_repo.packages
        if type(package) is recipe
        and isinstance(package, packages.BundledPackage)
        and package.sha256 is not None
    ]
    return tuple(sorted(releases, key=lambda package: package.version))


def selected_releases(
    config: ProjectConfig,
) -> tuple[packages.BundledPackage, ...]:
    releases = registered_releases(load_recipe(config.root_recipe))
    if not releases:
        raise ValueError(
            f"root recipe {config.root_recipe} has no registered releases"
        )
    if config.release_selection == "all":
        return releases
    if config.release_selection == "latest":
        return (releases[-1],)
    by_major: dict[int, list[packages.BundledPackage]] = defaultdict(list)
    for release in releases:
        by_major[release.version.major].append(release)
    return tuple(by_major[major][-1] for major in sorted(by_major))


def release_for(
    config: ProjectConfig, version_or_major: str
) -> packages.BundledPackage:
    releases = registered_releases(load_recipe(config.root_recipe))
    exact = [
        release
        for release in releases
        if release.source_version == version_or_major
    ]
    if exact:
        return exact[0]
    major = [
        release
        for release in releases
        if str(release.version.major) == version_or_major
    ]
    if major:
        return major[-1]
    raise ValueError(f"unsupported root release: {version_or_major}")


def selected_versions(config: ProjectConfig) -> tuple[str, ...]:
    return tuple(
        release.source_version for release in selected_releases(config)
    )


def _recipe_path(package: packages.BundledPackage) -> pathlib.Path:
    source = inspect.getsourcefile(type(package))
    if source is None:
        raise ValueError(f"recipe source not found for {type(package)!r}")
    return pathlib.Path(source).parent


def recipe_digest(package: packages.BundledPackage) -> str:
    return tree_digest(_recipe_path(package))


def _source_metadata(package: packages.BundledPackage) -> dict[str, str]:
    sources = package.get_sources()
    if len(sources) != 1 or package.sha256 is None:
        raise ValueError(
            f"registered release {package.name} must have one verified source"
        )
    return {
        "name": str(package.name),
        "recipe": f"{type(package).__module__}:{type(package).__qualname__}",
        "recipe_sha256": recipe_digest(package),
        "source_sha256": package.sha256,
        "source_url": sources[0].url,
        "version": package.source_version,
    }


def _registered_package(
    requirement: poetry_dependency.Dependency,
    preferred: dict[packages.NormalizedName, packages.BundledPackage]
    | None = None,
) -> packages.BundledPackage | None:
    preferred_packages = preferred or {}
    preferred_package = preferred_packages.get(
        requirement.name
    ) or preferred_packages.get(
        packages.canonicalize_name(requirement.name.removesuffix("-dev"))
    )
    if preferred_package is not None and requirement.constraint.allows(
        preferred_package.version
    ):
        return preferred_package
    matches = repository.bundle_repo.find_packages(requirement)
    matches.sort(key=lambda package: package.version, reverse=True)
    for match in matches:
        if isinstance(match, packages.BundledPackage):
            return match
        for alias_requirement in match.all_requires:
            resolved = _registered_package(alias_requirement, preferred)
            if resolved is not None:
                return resolved
    return None


def _resolved_requirements(
    requirements: Sequence[poetry_dependency.Dependency],
    preferred: dict[packages.NormalizedName, packages.BundledPackage]
    | None = None,
) -> tuple[packages.BundledPackage, ...]:
    resolved = {
        package.name: package
        for requirement in requirements
        if (package := _registered_package(requirement, preferred)) is not None
    }
    return tuple(resolved[name] for name in sorted(resolved))


def _direct_edges(
    package: packages.BundledPackage,
    preferred: dict[packages.NormalizedName, packages.BundledPackage],
) -> tuple[
    tuple[packages.BundledPackage, ...],
    tuple[packages.BundledPackage, ...],
]:
    return (
        _resolved_requirements(package.get_requirements(), preferred),
        _resolved_requirements(package.get_build_requirements(), preferred),
    )


def _package_closure(
    roots: Iterable[packages.BundledPackage],
) -> dict[str, packages.BundledPackage]:
    pending = list(roots)
    preferred = {package.name: package for package in pending}
    result: dict[str, packages.BundledPackage] = {}
    while pending:
        package = pending.pop()
        name = str(package.name)
        previous = result.get(name)
        if previous is not None:
            if previous.source_version != package.source_version:
                raise ValueError(
                    f"incompatible registered releases for {name}: "
                    f"{previous.source_version} and {package.source_version}"
                )
            continue
        result[name] = package
        runtime, build = _direct_edges(package, preferred)
        pending.extend((*runtime, *build))
        preferred.update({item.name: item for item in (*runtime, *build)})
    return result


def _node_id(role: str, target: str, package: str, version: str) -> str:
    raw = f"{role}-{target}-{package}-{version}".lower()
    return _NODE_SAFE.sub("_", raw).strip("_")


def _environment_identity(
    config: ProjectConfig, target_name: str
) -> dict[str, Any]:
    target = config.target(target_name)
    result: dict[str, Any] = {
        "environment": target.environment_dict,
        "execution": target.execution,
    }
    if target.execution == "docker":
        environment = docker_environment(target_name)
        result["docker"] = {
            "dockerfile_sha256": environment.dockerfile_sha256,
            "image": environment.image,
            "name": environment.name,
            "platform": environment.platform,
            "template": environment.template.name,
            "template_sha256": environment.template_sha256,
            "variables": environment.template_variables,
        }
    return result


def _project_identity(config: ProjectConfig) -> dict[str, str]:
    # @lat: [[orchestration#Project Orchestration#Canonical Build Plan#Cache Identity]]  # ruff: ignore[line-too-long]
    config_path = config.root / "pyproject.toml"
    if not config_path.is_file():
        return {
            "ggbuild_version": get_project_version_key(),
            "policy_sha256": tree_digest(config_path),
        }
    with config_path.open("rb") as source:
        policy = tomllib.load(source)
    identity_policy = copy.deepcopy(policy)
    raw_tool = identity_policy.get("tool")
    if isinstance(raw_tool, dict):
        raw_ggbuild = raw_tool.get("ggbuild")
        if isinstance(raw_ggbuild, dict):
            raw_ggbuild.pop("sccache", None)
    return {
        "ggbuild_version": get_project_version_key(),
        "policy_sha256": digest_json(identity_policy),
    }


def _closure(node_id: str, graph: dict[str, tuple[str, ...]]) -> list[str]:
    result: set[str] = set()

    def visit(current: str) -> None:
        if current in result:
            return
        result.add(current)
        for dependency in graph[current]:
            visit(dependency)

    visit(node_id)
    return sorted(result)


# @lat: [[orchestration#Project Orchestration#Canonical Build Plan]]
def create_build_plan(  # ruff: ignore[too-many-locals, too-many-statements]
    config: ProjectConfig | None = None,
    options: PlanOptions | None = None,
) -> BuildPlan:
    config = config or load_project()
    options = options or PlanOptions()
    targets = tuple(sorted(options.targets or tuple(config.target_map)))
    unknown = sorted(set(targets) - set(config.target_map))
    if unknown:
        raise ValueError(f"unsupported targets: {', '.join(unknown)}")
    releases = tuple(
        release_for(config, version)
        for version in (options.versions or selected_versions(config))
    )
    versions = tuple(release.source_version for release in releases)
    if len(set(versions)) != len(versions):
        raise ValueError("selected root releases must be unique")
    build_options = options.build or config.build_options
    common_options = build_options.as_dict()
    identity = _project_identity(config)

    nodes_by_id: dict[str, PlanNode] = {}
    node_index: dict[str, str] = {}
    graph: dict[str, tuple[str, ...]] = {}
    resolved_packages: dict[str, dict[str, dict[str, str]]] = {}
    root_ids: list[str] = []

    for target in targets:
        environment = _environment_identity(config, target)
        for release in releases:
            root_runtime = _resolved_requirements(release.get_requirements())
            root_build = _resolved_requirements(
                release.get_build_requirements()
            )
            closure = _package_closure((*root_runtime, *root_build))
            preferred = {package.name: package for package in closure.values()}
            metadata = {
                name: _source_metadata(package)
                for name, package in sorted(closure.items())
            }
            resolved_packages[release.source_version] = metadata
            direct_by_name = {
                name: _direct_edges(package, preferred)
                for name, package in closure.items()
            }
            ordering = {
                name: tuple(
                    sorted({
                        str(dependency.name)
                        for group in direct_by_name[name]
                        for dependency in group
                    })
                )
                for name in closure
            }
            dependency_nodes: dict[str, str] = {}
            for layer in topological_layers(ordering):
                for name in layer:
                    package = closure[name]
                    runtime, build = direct_by_name[name]
                    runtime_ids = tuple(
                        sorted(
                            dependency_nodes[str(item.name)] for item in runtime
                        )
                    )
                    build_ids = tuple(
                        sorted(
                            dependency_nodes[str(item.name)] for item in build
                        )
                    )
                    direct_ids = tuple(
                        sorted(set(runtime_ids) | set(build_ids))
                    )
                    inputs = {
                        "build_environment": environment,
                        "build_options": common_options,
                        "bundle_install_subdir": config.bundle_prefix,
                        "direct_dependency_keys": [
                            nodes_by_id[node_id].cache_key
                            for node_id in direct_ids
                        ],
                        "identity": identity,
                        "package": metadata[name],
                        "target": target,
                    }
                    cache_key = "ggbuild-v3-" + digest_json(inputs)
                    existing = node_index.get(cache_key)
                    if existing is not None:
                        dependency_nodes[name] = existing
                        continue
                    node_id = _node_id(
                        "bundle", target, name, package.source_version
                    )
                    if node_id in nodes_by_id:
                        node_id += "_" + digest_json(inputs)[:8]
                    node = PlanNode(
                        id=node_id,
                        role="bundle",
                        package=name,
                        recipe=metadata[name]["recipe"],
                        version=package.source_version,
                        target=target,
                        cache_key=cache_key,
                        direct_dependencies=direct_ids,
                        runtime_dependencies=runtime_ids,
                        build_dependencies=build_ids,
                        artifact_dependencies=(),
                        bundle_install_subdir=config.bundle_prefix,
                        installation_path=(
                            f"install/{target}/{name}-{package.source_version}"
                        ),
                        expected_outputs=(
                            {
                                "format": "ggbuild-bundle-v2",
                                "path": f"bundles/{cache_key}.tar.zst",
                            },
                        ),
                        inputs=inputs,
                    )
                    nodes_by_id[node_id] = node
                    node_index[cache_key] = node_id
                    dependency_nodes[name] = node_id
                    graph[node_id] = direct_ids

            root_runtime_ids = tuple(
                sorted(
                    dependency_nodes[str(package.name)]
                    for package in root_runtime
                )
            )
            root_build_ids = tuple(
                sorted(
                    dependency_nodes[str(package.name)]
                    for package in root_build
                )
            )
            root_dependencies = tuple(
                sorted(set(root_runtime_ids) | set(root_build_ids))
            )
            root_metadata = _source_metadata(release)
            inputs = {
                "build_environment": environment,
                "build_options": common_options,
                "bundle_install_subdir": config.bundle_prefix,
                "direct_dependency_keys": [
                    nodes_by_id[node_id].cache_key
                    for node_id in root_dependencies
                ],
                "identity": identity,
                "package": root_metadata,
                "target": target,
            }
            cache_key = "ggbuild-v3-" + digest_json(inputs)
            node_id = _node_id(
                "artifact", target, str(release.name), release.source_version
            )
            node = PlanNode(
                id=node_id,
                role="artifact",
                package=str(release.name),
                recipe=config.root_recipe,
                version=release.source_version,
                target=target,
                cache_key=cache_key,
                direct_dependencies=root_dependencies,
                runtime_dependencies=root_runtime_ids,
                build_dependencies=root_build_ids,
                artifact_dependencies=(),
                bundle_install_subdir=config.bundle_prefix,
                installation_path=None,
                expected_outputs=(
                    {
                        "format": "ggbuild-artifact-v2",
                        "path": f"artifacts/{target}/{release.source_version}/",
                    },
                ),
                inputs=inputs,
            )
            if node_id in nodes_by_id:
                raise ValueError(f"duplicate root node: {node_id}")
            nodes_by_id[node_id] = node
            graph[node_id] = root_dependencies
            recipe = type(release)
            if (
                recipe.get_test_script
                is packages.BundledPackage.get_test_script
            ):
                root_ids.append(node_id)
                continue
            test_inputs = {
                "artifact_key": cache_key,
                "identity": identity,
                "package": root_metadata,
                "target": target,
            }
            test_cache_key = "ggbuild-v3-" + digest_json(test_inputs)
            test_id = _node_id(
                "test", target, str(release.name), release.source_version
            )
            test_node = PlanNode(
                id=test_id,
                role="test",
                package=str(release.name),
                recipe=config.root_recipe,
                version=release.source_version,
                target=target,
                cache_key=test_cache_key,
                direct_dependencies=(node_id,),
                runtime_dependencies=(),
                build_dependencies=(),
                artifact_dependencies=(node_id,),
                bundle_install_subdir=config.bundle_prefix,
                installation_path=None,
                expected_outputs=(
                    {
                        "format": "ggbuild-test-result-v1",
                        "path": (
                            f"test-results/{target}/"
                            f"{release.source_version}.json"
                        ),
                    },
                ),
                inputs=test_inputs,
            )
            if test_id in nodes_by_id:
                raise ValueError(f"duplicate test node: {test_id}")
            nodes_by_id[test_id] = test_node
            graph[test_id] = (node_id,)
            root_ids.append(test_id)

    node_dicts = [
        nodes_by_id[node_id].as_dict() for node_id in sorted(nodes_by_id)
    ]
    roots: list[dict[str, Any]] = []
    for node_id in sorted(root_ids):
        node = nodes_by_id[node_id]
        root_closure = _closure(node_id, graph)
        roots.append({
            "closure": root_closure,
            "closure_digest": digest_json([
                nodes_by_id[item].cache_key for item in root_closure
            ]),
            "node_id": node_id,
            "target": node.target,
            "version": node.version,
        })
    raw: dict[str, Any] = {
        "build_options": common_options,
        "format_version": PLAN_FORMAT_VERSION,
        "layers": topological_layers(graph),
        "nodes": node_dicts,
        "project": config.project_name,
        "resolved_packages": resolved_packages,
        "roots": roots,
        "targets": list(targets),
    }
    validate_plan(raw)
    return BuildPlan.from_mapping(raw)


def create_plan(
    config: ProjectConfig | None = None,
    options: PlanOptions | None = None,
) -> dict[str, Any]:
    """Create the canonical JSON-compatible v3 plan."""
    return create_build_plan(config, options).as_dict()


def plan_for(
    *,
    targets: Iterable[str] = (),
    versions: Iterable[str] = (),
    config: ProjectConfig | None = None,
) -> dict[str, Any]:
    return create_plan(
        config,
        PlanOptions(targets=tuple(targets), versions=tuple(versions)),
    )
