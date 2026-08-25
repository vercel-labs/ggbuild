# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Bounded local scheduling for v3 plans."""

from __future__ import annotations

from typing import TYPE_CHECKING

import concurrent.futures
import dataclasses
import pathlib
import shlex
import shutil

from ggbuild.ci_protocol import (
    BundleError,
    ExecutionPaths,
    bundle_path,
    execute_node,
    export_bundle,
    node_map,
    restore_bundle,
    validate_plan,
    write_plan,
)
from ggbuild.execution import (
    build_command,
    node_environment,
    prepare_target,
    validate_prepared_target,
)
from ggbuild.node_cache import policy_for, unmatched_patterns
from ggbuild.node_executor import DefaultNodeExecutor, Executor
from ggbuild.planner import PlanOptions, create_plan
from ggbuild.project import ProjectConfig, load_project

if TYPE_CHECKING:
    from typing import Any

    from collections.abc import Mapping, Sequence


NodeCachePolicy = dict[str, str]


@dataclasses.dataclass(frozen=True, slots=True)
class RunOptions:
    targets: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    cache_dir: pathlib.Path = pathlib.Path(".cache")
    destination: pathlib.Path = pathlib.Path("dist")
    install_dir: pathlib.Path | None = None
    work_dir: pathlib.Path | None = None
    max_parallel: int = 1
    no_cache: bool = False
    node_cache: tuple[str, ...] = ()
    dry_run: bool = False
    root_nodes: tuple[str, ...] = ()
    node_ids: tuple[str, ...] = ()
    prepared_target: str | None = None
    exact_node: bool = False
    bare_linux_test: bool = False
    enable_sccache: bool = False


@dataclasses.dataclass(slots=True)
class RunSummary:
    cache_hits: list[str] = dataclasses.field(default_factory=list)
    cache_misses: list[str] = dataclasses.field(default_factory=list)
    built_nodes: list[str] = dataclasses.field(default_factory=list)
    reused_nodes: list[str] = dataclasses.field(default_factory=list)
    outputs: list[pathlib.Path] = dataclasses.field(default_factory=list)
    commands: list[list[str]] = dataclasses.field(default_factory=list)
    layers: list[list[str]] = dataclasses.field(default_factory=list)


class _ExecutorProvider:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor

    def build_node(
        self, node: Mapping[str, Any], paths: ExecutionPaths
    ) -> Sequence[pathlib.Path]:
        return self.executor(node, paths)


def parse_node_cache(values: Sequence[str]) -> NodeCachePolicy:
    """Parse repeatable RECIPE_GLOB={auto,ignore,reuse} entries."""
    result: NodeCachePolicy = {}
    for raw_value in values:
        for raw_entry in raw_value.split(","):
            entry = raw_entry.strip()
            recipe_glob, separator, raw_policy = entry.rpartition("=")
            policy = raw_policy.strip().lower()
            recipe_glob = recipe_glob.strip()
            if (
                not separator
                or not recipe_glob
                or policy not in {"auto", "ignore", "reuse"}
            ):
                raise ValueError(
                    "--node-cache entries must use "
                    "<recipe-glob>={auto,ignore,reuse}"
                )
            if (
                "?" in recipe_glob
                or "[" in recipe_glob
                or "]" in recipe_glob
                or "*" in recipe_glob.removesuffix("*")
            ):
                raise ValueError(
                    "--node-cache recipe globs may only use one trailing '*'"
                )
            # Reinsert duplicates so mapping order preserves CLI precedence.
            result.pop(recipe_glob, None)
            result[recipe_glob] = policy
    return result


def _is_cache_hit(
    node: Mapping[str, Any], cache_dir: pathlib.Path, install_dir: pathlib.Path
) -> bool:
    cached = bundle_path(cache_dir, node["cache_key"])
    if not cached.is_file():
        return False
    try:
        restore_bundle(
            cached,
            install_dir / str(node["installation_path"]),
            node=node,
        )
    except BundleError:
        return False
    return True


def _reuse_node_cache(
    node: Mapping[str, Any],
    cache_dir: pathlib.Path,
    install_dir: pathlib.Path,
) -> bool:
    bundles = cache_dir / "bundles"
    if not bundles.is_dir():
        return False
    current = bundle_path(cache_dir, node["cache_key"])
    candidates = sorted(
        (
            path
            for path in bundles.glob("ggbuild-v3-*.tar.zst")
            if path != current
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    installation = install_dir / str(node["installation_path"])
    for candidate in candidates:
        try:
            restore_bundle(
                candidate,
                installation,
                node=node,
                allow_cache_key_mismatch=True,
            )
        except BundleError:
            continue
        export_bundle(installation, current, node=node)
        return True
    return False


def _selected_nodes(
    plan: Mapping[str, Any],
    root_nodes: Sequence[str],
    node_ids: Sequence[str],
    *,
    exact_node: bool = False,
) -> set[str]:
    if exact_node and (root_nodes or len(node_ids) != 1):
        raise ValueError("exact node execution requires exactly one node")
    if not root_nodes and not node_ids:
        return {node["id"] for node in plan["nodes"]}
    roots = {root["node_id"]: root for root in plan["roots"]}
    unknown = sorted(set(root_nodes) - set(roots))
    if unknown:
        raise ValueError(f"unknown root nodes: {', '.join(unknown)}")
    selected = {
        node_id
        for root_id in root_nodes
        for node_id in roots[root_id]["closure"]
    }
    nodes = node_map(plan)
    unknown = sorted(set(node_ids) - set(nodes))
    if unknown:
        raise ValueError(f"unknown build plan nodes: {', '.join(unknown)}")
    if exact_node:
        return set(node_ids)

    def add_closure(node_id: str) -> None:
        if node_id in selected:
            return
        selected.add(node_id)
        for dependency_id in nodes[node_id]["direct_dependencies"]:
            add_closure(dependency_id)

    for node_id in node_ids:
        add_closure(node_id)
    return selected


def run_plan(  # ruff: ignore[too-many-branches, too-many-locals, too-many-statements]
    plan: Mapping[str, Any],
    options: RunOptions,
    *,
    config: ProjectConfig | None = None,
    executor: Executor | None = None,
) -> RunSummary:
    validate_plan(plan)
    if options.max_parallel < 1:
        raise ValueError("--max-parallel must be at least 1")
    config = config or load_project()
    nodes = node_map(plan)
    selected = _selected_nodes(
        plan,
        options.root_nodes,
        options.node_ids,
        exact_node=options.exact_node,
    )
    if options.bare_linux_test:
        if options.prepared_target is not None:
            raise ValueError(
                "bare Linux testing cannot use a prepared build target"
            )
        if not options.exact_node or len(selected) != 1:
            raise ValueError("bare Linux testing requires exact node execution")
        selected_node = nodes[next(iter(selected))]
        if selected_node["role"] != "test" or "-linux-" not in str(
            selected_node["target"]
        ):
            raise ValueError("bare Linux testing requires a Linux test node")
    node_cache = parse_node_cache(options.node_cache)
    selected_recipes = {str(nodes[node_id]["recipe"]) for node_id in selected}
    bundle_recipes = {
        str(nodes[node_id]["recipe"])
        for node_id in selected
        if nodes[node_id]["role"] == "bundle"
    }
    unknown_cache_nodes = unmatched_patterns(node_cache, selected_recipes)
    if unknown_cache_nodes:
        raise ValueError(
            "--node-cache references nodes outside the selected closure: "
            + ", ".join(unknown_cache_nodes)
        )
    non_bundle_policies = sorted(
        recipe_glob
        for recipe_glob, policy in node_cache.items()
        if policy != "auto"
        and not any(
            policy_for({recipe_glob: policy}, (recipe,)) is not None
            for recipe in bundle_recipes
        )
    )
    if non_bundle_policies:
        raise ValueError(
            "--node-cache only applies to bundle nodes: "
            + ", ".join(non_bundle_policies)
        )
    if options.no_cache and any(
        policy == "reuse" for policy in node_cache.values()
    ):
        raise ValueError("--no-cache conflicts with --node-cache=...=reuse")
    selected_targets = {str(nodes[node_id]["target"]) for node_id in selected}
    if options.prepared_target is not None:
        if selected_targets != {options.prepared_target}:
            actual = ", ".join(sorted(selected_targets)) or "none"
            raise ValueError(
                f"selected closure targets ({actual}) do not match prepared "
                f"target {options.prepared_target}"
            )
        validate_prepared_target(config.target(options.prepared_target))
    provider = (
        _ExecutorProvider(executor)
        if executor
        else DefaultNodeExecutor(
            config,
            prepared_target=options.prepared_target,
            bare_linux_test=options.bare_linux_test,
            enable_sccache=options.enable_sccache,
        )
    )
    layers = [
        [node_id for node_id in layer if node_id in selected]
        for layer in plan["layers"]
    ]
    layers = [layer for layer in layers if layer]
    summary = RunSummary(layers=layers)
    cache_dir = options.cache_dir.resolve()
    destination = options.destination.resolve()
    install_dir = (
        options.install_dir.resolve()
        if options.install_dir is not None
        else cache_dir / "install"
    )
    work_root = (
        options.work_dir.resolve()
        if options.work_dir is not None
        else cache_dir / "work"
    )
    plan_path = cache_dir / "plan.json"
    if not options.dry_run:
        cache_dir.mkdir(parents=True, exist_ok=True)
        destination.mkdir(parents=True, exist_ok=True)
        write_plan(plan_path, plan)
        if options.prepared_target is None and not options.bare_linux_test:
            for target_name in sorted(selected_targets):
                prepare_target(config, target_name)

    def paths_for(node_id: str, work_dir: pathlib.Path) -> ExecutionPaths:
        return ExecutionPaths(
            bundle_dir=cache_dir,
            install_dir=install_dir,
            output_dir=destination,
            work_dir=work_dir,
        )

    def build(node_id: str) -> tuple[str, tuple[pathlib.Path, ...]]:
        work_root.mkdir(parents=True, exist_ok=True)
        work_dir = work_root / node_id
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            outputs = execute_node(
                plan_path,
                node_id,
                paths_for(node_id, work_dir),
                provider,
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        return node_id, outputs

    for layer in layers:
        to_build: list[str] = []
        for node_id in layer:
            node = nodes[node_id]
            preview_work = work_root / node_id
            paths = paths_for(node_id, preview_work)
            environment = node_environment(node, paths, config)
            summary.commands.append(
                build_command(
                    node,
                    paths,
                    config,
                    environment=environment,
                    prepared_target=options.prepared_target,
                    bare_linux_test=options.bare_linux_test,
                )
            )
            cache_policy = policy_for(node_cache, (str(node["recipe"]),))
            use_cache = (
                node["role"] == "bundle"
                and not options.no_cache
                and cache_policy != "ignore"
            )
            if use_cache:
                cached = (
                    bundle_path(cache_dir, node["cache_key"]).is_file()
                    if options.dry_run
                    else _is_cache_hit(node, cache_dir, install_dir)
                )
                if (
                    not cached
                    and cache_policy == "reuse"
                    and not options.dry_run
                ):
                    cached = _reuse_node_cache(node, cache_dir, install_dir)
                if cached:
                    summary.cache_hits.append(node_id)
                    summary.reused_nodes.append(node_id)
                    continue
            if node["role"] == "bundle":
                summary.cache_misses.append(node_id)
            to_build.append(node_id)
        if options.dry_run:
            continue
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=options.max_parallel
        ) as pool:
            futures = {
                pool.submit(build, node_id): node_id for node_id in to_build
            }
            try:
                for future in concurrent.futures.as_completed(futures):
                    node_id, outputs = future.result()
                    summary.built_nodes.append(node_id)
                    summary.outputs.extend(outputs)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
    summary.built_nodes.sort()
    summary.cache_hits.sort()
    summary.cache_misses.sort()
    summary.reused_nodes.sort()
    summary.outputs.sort()
    return summary


def run_local(
    options: RunOptions,
    *,
    config: ProjectConfig | None = None,
    executor: Executor | None = None,
) -> RunSummary:
    config = config or load_project()
    plan = create_plan(
        config,
        PlanOptions(targets=options.targets, versions=options.versions),
    )
    return run_plan(plan, options, config=config, executor=executor)


def format_summary(summary: RunSummary, *, dry_run: bool = False) -> str:
    lines = [
        f"cache hits: {len(summary.cache_hits)}",
        f"cache misses: {len(summary.cache_misses)}",
        f"built nodes: {len(summary.built_nodes)}",
        f"reused nodes: {len(summary.reused_nodes)}",
        f"outputs: {len(summary.outputs)}",
    ]
    if dry_run:
        lines.append("layers:")
        lines.extend(
            f"  {index}: {', '.join(layer)}"
            for index, layer in enumerate(summary.layers)
        )
        lines.append("commands:")
        lines.extend(f"  {shlex.join(command)}" for command in summary.commands)
    else:
        lines.extend(f"output: {path}" for path in summary.outputs)
    return "\n".join(lines)
