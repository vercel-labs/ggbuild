# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Declarative project configuration for ggbuild orchestration."""

from __future__ import annotations

from typing import Any, Literal, cast

import dataclasses
import pathlib
import re
import tomllib

from ggbuild.targets.linux.dockerfile import docker_environment
from ggbuild.targets.policy import ExecutionMode, target_policy

ReleaseSelection = Literal["all", "latest", "latest-per-major"]


@dataclasses.dataclass(frozen=True, slots=True)
class BuildOptions:
    revision: str = "1"
    build_source: bool = False
    build_dbgsym: bool = False
    release: bool = False
    extra_optimizations: bool = False
    compression: tuple[str, ...] = ("zstd",)

    def as_dict(self) -> dict[str, object]:
        return {
            "build_dbgsym": self.build_dbgsym,
            "build_source": self.build_source,
            "compression": list(self.compression),
            "extra_optimizations": self.extra_optimizations,
            "release": self.release,
            "revision": self.revision,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class TargetConfig:
    triple: str
    execution: ExecutionMode
    runner: str
    environment: tuple[tuple[str, str], ...] = ()

    @property
    def environment_dict(self) -> dict[str, str]:
        return dict(self.environment)


@dataclasses.dataclass(frozen=True, slots=True)
class WorkflowConfig:
    name: str = "ggbuild"
    path: pathlib.Path = pathlib.Path(".github/workflows/ggbuild.yml")
    setup_action: str = "./.github/actions/ggbuild-setup"
    branch: str = "main"
    max_concurrency: int = 12
    artifact_name: str = "{package}-{target}-{version}"


@dataclasses.dataclass(frozen=True, slots=True)
class SccacheConfig:
    production: bool = False
    pull_request: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class PublicationConfig:
    repository: str
    index_url: str | None = None
    protection_bypass_secret: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectConfig:
    root: pathlib.Path
    project_name: str
    root_recipe: str
    release_selection: ReleaseSelection
    portable: bool
    bundle_prefix: str
    targets: tuple[TargetConfig, ...]
    workflow: WorkflowConfig
    build_options: BuildOptions
    sccache: SccacheConfig = SccacheConfig()
    publication: PublicationConfig | None = None

    @property
    def target_map(self) -> dict[str, TargetConfig]:
        return {target.triple: target for target in self.targets}

    def target(self, triple: str) -> TargetConfig:
        try:
            return self.target_map[triple]
        except KeyError as error:
            raise ValueError(f"unsupported target: {triple}") from error


def find_project_root(start: pathlib.Path | None = None) -> pathlib.Path:
    current = (start or pathlib.Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        path = candidate / "pyproject.toml"
        if path.is_file():
            with path.open("rb") as source:
                data = tomllib.load(source)
            if isinstance(data.get("tool", {}).get("ggbuild"), dict):
                return candidate
    raise ValueError("no pyproject.toml with [tool.ggbuild] found")


def _string_mapping(
    value: object, *, field: str
) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise TypeError(f"{field} must be a string table")
    return tuple(sorted(cast("dict[str, str]", value).items()))


def _target(raw: object) -> TargetConfig:
    if not isinstance(raw, dict):
        raise TypeError("tool.ggbuild.target entries must be tables")
    value = cast("dict[str, Any]", raw)
    triple = value.get("triple")
    if not isinstance(triple, str) or not triple:
        raise TypeError("each target needs a non-empty triple")
    defaults = target_policy(triple)
    execution = value.get("execution", defaults.execution)
    runner = value.get("runner", defaults.runner)
    if execution not in {"host", "docker"}:
        raise ValueError(f"target {triple} has invalid execution mode")
    if not isinstance(runner, str) or not runner:
        raise TypeError(f"target {triple} needs an Actions runner")
    if execution == "docker":
        docker_environment(triple)
    environment = {
        **dict(defaults.environment),
        **dict(
            _string_mapping(
                value.get("environment"),
                field=f"target {triple} environment",
            )
        ),
    }
    return TargetConfig(
        triple=triple,
        execution=execution,
        runner=runner,
        environment=tuple(sorted(environment.items())),
    )


# @lat: [[orchestration#Project Orchestration#Project and Target Policy]]
def load_project(  # ruff: ignore[too-many-branches, too-many-locals,too-many-statements]
    root: pathlib.Path | None = None,
) -> ProjectConfig:
    project_root = (root or find_project_root()).resolve()
    with (project_root / "pyproject.toml").open("rb") as source:
        document = tomllib.load(source)
    raw = document.get("tool", {}).get("ggbuild")
    if not isinstance(raw, dict):
        raise TypeError("pyproject.toml has no [tool.ggbuild] table")
    config = cast("dict[str, Any]", raw)
    recipe = config.get("root-recipe")
    if not isinstance(recipe, str) or ":" not in recipe:
        raise ValueError("tool.ggbuild.root-recipe must use module:Class form")
    selection = config.get("release-selection", "latest-per-major")
    if selection not in {"all", "latest", "latest-per-major"}:
        raise ValueError("tool.ggbuild.release-selection is invalid")
    bundle_prefix = config.get("bundle-prefix")
    if not isinstance(bundle_prefix, str) or not bundle_prefix:
        raise TypeError("tool.ggbuild.bundle-prefix must be a non-empty string")
    raw_targets = config.get("target")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("tool.ggbuild.target must contain at least one target")
    targets = tuple(_target(item) for item in raw_targets)
    if len({target.triple for target in targets}) != len(targets):
        raise ValueError("tool.ggbuild.target contains duplicate triples")
    raw_workflow = config.get("workflow", {})
    if not isinstance(raw_workflow, dict):
        raise TypeError("tool.ggbuild.workflow must be a table")
    workflow_value = cast("dict[str, Any]", raw_workflow)
    workflow = WorkflowConfig(
        name=str(workflow_value.get("name", "ggbuild")),
        path=pathlib.Path(
            str(workflow_value.get("path", ".github/workflows/ggbuild.yml"))
        ),
        setup_action=str(
            workflow_value.get(
                "setup-action", "./.github/actions/ggbuild-setup"
            )
        ),
        branch=str(workflow_value.get("branch", "main")),
        max_concurrency=int(workflow_value.get("max-concurrency", 12)),
        artifact_name=str(
            workflow_value.get("artifact-name", "{package}-{target}-{version}")
        ),
    )
    if workflow.max_concurrency < 1:
        raise ValueError(
            "tool.ggbuild.workflow.max-concurrency must be positive"
        )
    raw_build = config.get("build", {})
    if not isinstance(raw_build, dict):
        raise TypeError("tool.ggbuild.build must be a table")
    build_value = cast("dict[str, Any]", raw_build)
    if "build-debug" in build_value:
        raise ValueError(
            "tool.ggbuild.build.build-debug is unsupported; use build-dbgsym"
        )
    raw_compression = build_value.get("compression", ["zstd"])
    if not isinstance(raw_compression, list) or not all(
        isinstance(item, str) for item in raw_compression
    ):
        raise TypeError("tool.ggbuild.build.compression must be a string array")
    build_options = BuildOptions(
        revision=str(build_value.get("revision", "1")),
        build_source=bool(build_value.get("build-source", False)),
        build_dbgsym=bool(build_value.get("build-dbgsym", False)),
        release=bool(build_value.get("release", False)),
        extra_optimizations=bool(build_value.get("extra-optimizations", False)),
        compression=tuple(raw_compression),
    )
    raw_sccache = config.get("sccache", {})
    if not isinstance(raw_sccache, dict):
        raise TypeError("tool.ggbuild.sccache must be a table")
    sccache_value = cast("dict[str, Any]", raw_sccache)
    for key in ("production", "pull-request"):
        if key in sccache_value and not isinstance(sccache_value[key], bool):
            raise TypeError(f"tool.ggbuild.sccache.{key} must be a boolean")
    sccache = SccacheConfig(
        production=sccache_value.get("production", False),
        pull_request=sccache_value.get("pull-request", True),
    )
    raw_publication = config.get("publication")
    publication = None
    if raw_publication is not None:
        if not isinstance(raw_publication, dict):
            raise TypeError("tool.ggbuild.publication must be a table")
        publication_value = cast("dict[str, Any]", raw_publication)
        repository = publication_value.get("repository")
        if not isinstance(repository, str) or repository.count("/") != 1:
            raise ValueError(
                "tool.ggbuild.publication.repository must use owner/name form"
            )
        index_url = publication_value.get("index-url")
        if index_url is not None and (
            not isinstance(index_url, str)
            or not index_url.startswith("https://")
        ):
            raise ValueError(
                "tool.ggbuild.publication.index-url must be an HTTPS URL"
            )
        protection_bypass_secret = publication_value.get(
            "protection-bypass-secret"
        )
        if protection_bypass_secret is not None and (
            not isinstance(protection_bypass_secret, str)
            or re.fullmatch(
                r"(?!GITHUB_)[A-Za-z_][A-Za-z0-9_]*",
                protection_bypass_secret,
                flags=re.IGNORECASE,
            )
            is None
        ):
            raise ValueError(
                "tool.ggbuild.publication.protection-bypass-secret must be "
                "a GitHub Actions secret name"
            )
        publication = PublicationConfig(
            repository, index_url, protection_bypass_secret
        )
    project_table = document.get("project", {})
    project_name = (
        str(project_table.get("name", project_root.name))
        if isinstance(project_table, dict)
        else project_root.name
    )
    return ProjectConfig(
        root=project_root,
        project_name=project_name,
        root_recipe=recipe,
        release_selection=cast("ReleaseSelection", selection),
        portable=bool(config.get("portable", True)),
        bundle_prefix=bundle_prefix,
        targets=targets,
        workflow=workflow,
        build_options=build_options,
        sccache=sccache,
        publication=publication,
    )
