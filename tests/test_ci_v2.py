# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import Any, ClassVar, cast

import dataclasses
import hashlib
import io
import pathlib
import tarfile
import threading
from collections.abc import Mapping
from operator import itemgetter
from unittest import mock

import pytest
import yaml

from ggbuild import execution, packages as ggbuild_packages
from ggbuild.ci_protocol import (
    PLAN_FORMAT_VERSION,
    BundleError,
    ExecutionPaths,
    bundle_path,
    digest_json,
    export_bundle,
    merge_bundle,
    restore_bundle,
    topological_layers,
    validate_plan,
)
from ggbuild.execution import (
    build_command,
    node_environment,
    prepare_target,
    run_docker_build,
    run_docker_container,
    validate_execution,
    validate_prepared_target,
)
from ggbuild.node_cache import policy_for
from ggbuild.node_executor import DefaultNodeExecutor
from ggbuild.packages import sources as package_sources
from ggbuild.planner import create_plan
from ggbuild.project import (
    BuildOptions,
    ProjectConfig,
    PublicationConfig,
    SccacheConfig,
    TargetConfig,
    WorkflowConfig,
    load_project,
)
from ggbuild.scheduler import (
    RunOptions,
    parse_node_cache,
    run_plan,
)
from ggbuild.targets.linux.dockerfile import (
    action_entrypoint,
    bare_test_dockerfile,
    bare_test_image,
    bare_test_source_sha256,
    docker_environment,
    dockerfile_template,
    write_bare_test_context,
)
from ggbuild.targets.policy import target_policy
from ggbuild.updater import (
    UpdateableBundledCAutoconfPackage,
    UpdatePolicy,
    latest_version,
    rewrite_declarations,
    update_releases,
    write_atomic,
    write_transaction,
)
from ggbuild.workflow import ggbuild_source_revision, render_workflow

TARGET = "x86_64-unknown-linux-gnu"


def fixture_config(root: pathlib.Path) -> ProjectConfig:
    return ProjectConfig(
        root=root,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "host", "ubuntu-latest"),),
        workflow=WorkflowConfig(max_concurrency=2),
        build_options=BuildOptions(),
    )


def test_generated_workflow_can_checkout_repository(
    tmp_path: pathlib.Path,
) -> None:
    workflow = render_workflow(
        fixture_config(tmp_path), ggbuild_revision="a" * 40
    )

    assert "permissions:\n  contents: read\n" in workflow


def test_generated_workflow_applies_sccache_event_policy(
    tmp_path: pathlib.Path,
) -> None:
    config = dataclasses.replace(
        fixture_config(tmp_path),
        sccache=SccacheConfig(production=True, pull_request=False),
    )
    docker_target = dataclasses.replace(config.targets[0], execution="docker")
    workflow = render_workflow(
        dataclasses.replace(config, targets=(docker_target,)),
        ggbuild_revision="a" * 40,
    )
    document = yaml.safe_load(workflow)

    dispatch = document[True]["workflow_dispatch"]["inputs"]
    assert dispatch["enable_sccache"]["default"] is True
    assert "github.event_name == 'pull_request' && false" in workflow
    assert "github.event_name == 'push' && true" in workflow
    assert ("(github.event_name == 'push' && true) }}\n\njobs:") in workflow
    assert "Get sccache credentials" in workflow
    assert "--env ACTIONS_RUNTIME_TOKEN" in workflow
    assert "${{ env.ACTIONS_RUNTIME_TOKEN }}" not in workflow


def test_workflow_revision_uses_distribution_origin() -> None:
    revision = "a" * 40
    with mock.patch(
        "ggbuild.workflow.get_origin_commit_id", return_value=revision
    ):
        assert ggbuild_source_revision() == revision


@pytest.mark.parametrize("revision", [None, "a" * 39, "main"])
def test_workflow_revision_requires_full_commit(revision: str | None) -> None:
    with (
        mock.patch(
            "ggbuild.workflow.get_origin_commit_id", return_value=revision
        ),
        pytest.raises(ValueError, match="full Git commit"),
    ):
        ggbuild_source_revision()


def test_planner_selects_latest_per_major_and_deduplicates(
    tmp_path: pathlib.Path,
) -> None:
    config = fixture_config(tmp_path)
    first = create_plan(config)
    second = create_plan(config)

    assert first == second
    assert first["format_version"] == 3
    assert [root["version"] for root in first["roots"]] == ["1.2", "2.0"]
    bundles = [node for node in first["nodes"] if node["role"] == "bundle"]
    assert len(bundles) == 1
    assert bundles[0]["package"] == "v2-dependency"
    assert all(
        node["cache_key"].startswith("ggbuild-v3-") for node in first["nodes"]
    )


def test_sccache_policy_does_not_change_plan_cache_identity(
    tmp_path: pathlib.Path,
) -> None:
    config = fixture_config(tmp_path)
    config_file = tmp_path / "pyproject.toml"
    config_file.write_text("[tool.ggbuild.sccache]\nproduction = false\n")
    first = create_plan(config)
    config_file.write_text("[tool.ggbuild.sccache]\nproduction = true\n")
    second = create_plan(config)

    assert [node["cache_key"] for node in first["nodes"]] == [
        node["cache_key"] for node in second["nodes"]
    ]


def test_generated_workflow_maps_exact_dependencies_to_bounded_matrices(
    tmp_path: pathlib.Path,
) -> None:
    workflow = render_workflow(
        fixture_config(tmp_path), ggbuild_revision="a" * 40
    )

    document = yaml.safe_load(workflow)
    jobs = document["jobs"]
    matrices = {
        name: job for name, job in jobs.items() if name.startswith("nodes_")
    }
    assert set(jobs) == {"planning", *matrices}
    assert all(
        job["strategy"]["max-parallel"] == 2 for job in matrices.values()
    )
    assert all(
        job["strategy"]["fail-fast"] is False for job in matrices.values()
    )
    node_jobs = {
        entry["node"]: name
        for name, job in matrices.items()
        for entry in job["strategy"]["matrix"]["include"]
    }
    plan = create_plan(fixture_config(tmp_path))
    plan_nodes = {str(node["id"]): node for node in plan["nodes"]}
    for job in matrices.values():
        node_ids = [
            entry["node"] for entry in job["strategy"]["matrix"]["include"]
        ]
        assert len({plan_nodes[node_id]["target"] for node_id in node_ids}) == 1
        expected = sorted({
            node_jobs[str(dependency)]
            for node_id in node_ids
            for dependency in plan_nodes[node_id]["direct_dependencies"]
        })
        assert sorted(job["needs"]) == (expected or ["planning"])
    entries = [
        entry
        for job in matrices.values()
        for entry in job["strategy"]["matrix"]["include"]
    ]
    assert {entry["node"] for entry in entries} == {
        str(node["id"]) for node in plan["nodes"]
    }
    assert len(entries) == len(plan["nodes"])
    runs = "\n".join(
        step["run"]
        for job in matrices.values()
        for step in job["steps"]
        if "run" in step
    )
    assert "ci execute-node" in runs
    assert "ci execute-target" not in runs
    assert "--max-parallel 1" in runs
    assert "--exact" in runs


def test_generated_workflow_cancels_only_obsolete_pull_request_runs(
    tmp_path: pathlib.Path,
) -> None:
    document = yaml.safe_load(
        render_workflow(fixture_config(tmp_path), ggbuild_revision="a" * 40)
    )

    assert document["concurrency"] == {
        "group": (
            "${{ github.workflow }}-"
            "${{ github.event.pull_request.number || github.ref }}"
        ),
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


def test_generated_publication_job_requires_every_matrix_to_succeed(
    tmp_path: pathlib.Path,
) -> None:
    config = dataclasses.replace(
        fixture_config(tmp_path),
        publication=PublicationConfig(
            "vercel-labs/postgresbuild",
            "https://postgresbuild.labs.vercel.dev/api/publication",
            "VERCEL_AUTOMATION_BYPASS_SECRET",
        ),
    )
    workflow = render_workflow(config, ggbuild_revision="a" * 40)
    document = yaml.safe_load(workflow)
    publish = document["jobs"]["publish"]
    ingest = document["jobs"]["ingest_existing"]

    assert publish["environment"] == "release"
    assert publish["permissions"] == {
        "actions": "read",
        "contents": "write",
        "id-token": "write",
    }
    assert publish["if"].startswith(
        "success() && github.repository == 'vercel-labs/postgresbuild'"
    )
    assert set(publish["needs"]) == {
        "planning",
        *(name for name, job in document["jobs"].items() if "strategy" in job),
    }
    assert "github.event_name == 'push'" in publish["if"]
    assert "github.event_name == 'workflow_dispatch'" in publish["if"]
    assert "inputs.operation == 'build-and-publish'" in publish["if"]
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    downloads = [
        step
        for step in publish["steps"]
        if step.get("name") == "Download every planned root"
    ]
    assert downloads[0]["with"] == {
        "pattern": "ggbuild-publication-*",
        "path": ".cache/publication",
    }
    publish_step = next(
        step for step in publish["steps"] if step.get("id") == "publish"
    )
    assert publish_step["env"] == {
        "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"
    }
    ingestion = publish["steps"][-1]
    assert "core.getIDToken" in ingestion["with"]["script"]
    assert ingestion["env"]["PUBLICATION_PROTECTION_BYPASS"] == (
        "${{ secrets.VERCEL_AUTOMATION_BYPASS_SECRET }}"
    )
    assert "'x-vercel-protection-bypass'" in ingestion["with"]["script"]
    assert ingestion["env"]["PUBLICATION_INDEX_URL"].endswith(
        "/api/publication"
    )
    github_token = ingestion["env"]["PUBLICATION_GITHUB_TOKEN"]
    assert github_token.startswith("${{ secrets.")
    assert github_token.endswith(" }}")
    assert "'x-github-token'" in ingestion["with"]["script"]
    assert "result.ignored === true" in ingestion["with"]["script"]
    root_uploads = [
        step
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Publish tested root artifact"
    ]
    assert root_uploads
    assert all(
        step["if"] == "matrix.role == 'test' && matrix.publish_root"
        for step in root_uploads
    )
    assert all(step["with"]["retention-days"] == 1 for step in root_uploads)
    assert document[True]["workflow_dispatch"]["inputs"]["operation"] == {
        "description": "Build a new snapshot or ingest an existing release",
        "type": "choice",
        "options": ["build-and-publish", "ingest-existing"],
        "default": "build-and-publish",
    }
    assert "ingest-existing" in document["jobs"]["planning"]["if"]
    assert all(
        "ingest-existing" in job["if"]
        for job in document["jobs"].values()
        if "strategy" in job
    )
    assert ingest["environment"] == "release"
    assert ingest["permissions"] == {"contents": "read", "id-token": "write"}
    assert "inputs.operation == 'ingest-existing'" in ingest["if"]
    assert "^[0-9]{12}$" in ingest["steps"][0]["run"]
    assert ingest["steps"][1]["env"]["PUBLICATION_TAG"] == "${{ inputs.tag }}"
    assert ingest["steps"][1]["env"]["PUBLICATION_PROTECTION_BYPASS"] == (
        "${{ secrets.VERCEL_AUTOMATION_BYPASS_SECRET }}"
    )
    assert (
        "'x-vercel-protection-bypass'" in ingest["steps"][1]["with"]["script"]
    )


def test_projects_without_publication_do_not_upload_test_records(
    tmp_path: pathlib.Path,
) -> None:
    document = yaml.safe_load(
        render_workflow(fixture_config(tmp_path), ggbuild_revision="a" * 40)
    )
    assert "publish" not in document["jobs"]
    entries = [
        entry
        for job in document["jobs"].values()
        if "strategy" in job
        for entry in job["strategy"]["matrix"]["include"]
        if entry["role"] == "test"
    ]
    assert entries
    assert all(entry["publish_root"] is False for entry in entries)
    assert all(not entry["upload_name"] for entry in entries)


def test_generated_musl_tests_pull_minimal_runtime_image(
    tmp_path: pathlib.Path,
) -> None:
    config = dataclasses.replace(
        fixture_config(tmp_path),
        targets=(
            TargetConfig("x86_64-unknown-linux-musl", "host", "ubuntu-latest"),
        ),
    )
    workflow = render_workflow(config, ggbuild_revision="a" * 40)

    document = yaml.safe_load(workflow)
    jobs = [
        job
        for name, job in document["jobs"].items()
        if name.startswith("nodes_")
    ]
    entries = [
        entry for job in jobs for entry in job["strategy"]["matrix"]["include"]
    ]
    test_entry = next(entry for entry in entries if entry["role"] == "test")
    assert test_entry["test_image"].startswith(
        "ghcr.io/vercel-labs/ggbuild/test-linux-musl:"
    )
    pull = next(
        step
        for step in jobs[-1]["steps"]
        if step.get("name") == "Pull Linux runtime-test image"
    )
    assert pull["run"] == 'docker pull "${{ matrix.test_image }}"'


def test_generated_docker_jobs_authenticate_to_ghcr(
    tmp_path: pathlib.Path,
) -> None:
    config = fixture_config(tmp_path)
    docker_config = dataclasses.replace(
        config,
        targets=(dataclasses.replace(config.targets[0], execution="docker"),),
    )
    workflow = render_workflow(docker_config, ggbuild_revision="a" * 40)

    assert "packages: read" in workflow
    assert "docker/login-action@" in workflow
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "docker run --rm" in workflow
    assert "uses: docker://" not in workflow


def test_exact_node_execution_does_not_schedule_its_closure(
    tmp_path: pathlib.Path,
) -> None:
    bundle = _node("bundle_a", "bundle")
    artifact = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    plan = _plan([bundle, artifact])
    calls: list[str] = []
    cache = tmp_path / "cache"
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "dependency").write_text("bundle", encoding="utf-8")
    export_bundle(
        staged,
        bundle_path(cache, str(bundle["cache_key"])),
        node=bundle,
    )

    def fake(
        node: Mapping[str, Any], paths: ExecutionPaths
    ) -> tuple[pathlib.Path, ...]:
        calls.append(str(node["id"]))
        output = paths.output_dir / TARGET / "1.0"
        output.mkdir(parents=True, exist_ok=True)
        archive = output / "root.tar.zst"
        archive.write_text("root", encoding="utf-8")
        return (archive,)

    with mock.patch("ggbuild.scheduler.prepare_target"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=cache,
                destination=tmp_path / "output",
                node_ids=("artifact_root",),
                exact_node=True,
            ),
            config=fixture_config(tmp_path),
            executor=fake,
        )

    assert calls == ["artifact_root"]


def test_bare_linux_test_requires_one_exact_linux_test_node(
    tmp_path: pathlib.Path,
) -> None:
    artifact = _node("artifact_root", "artifact")
    test = _node(
        "test_root",
        "test",
        dependencies=("artifact_root",),
        artifact_dependencies=("artifact_root",),
    )
    plan = _plan([artifact, test])
    with pytest.raises(ValueError, match="requires exact node"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                node_ids=("test_root",),
                bare_linux_test=True,
                dry_run=True,
            ),
            config=fixture_config(tmp_path),
        )
    result = run_plan(
        plan,
        RunOptions(
            cache_dir=tmp_path / "cache",
            destination=tmp_path / "output",
            node_ids=("test_root",),
            exact_node=True,
            bare_linux_test=True,
            dry_run=True,
        ),
        config=fixture_config(tmp_path),
    )
    assert result.layers == [["test_root"]]
    assert result.commands[0][-1] == "--bare-linux"


def test_bare_linux_test_bypasses_project_build_container(
    tmp_path: pathlib.Path,
) -> None:
    test = _node("test_root", "test")
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "cache",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    raw = paths.work_dir / "raw-output"
    raw.mkdir(parents=True)
    (raw / "test-result.json").write_text("{}", encoding="utf-8")
    executor = DefaultNodeExecutor(
        fixture_config(tmp_path), bare_linux_test=True
    )
    with (
        mock.patch("ggbuild.node_executor.run_docker_container") as docker,
        mock.patch("ggbuild.node_executor.run_child_build") as child,
    ):
        executor.build_node(test, paths)

    docker.assert_not_called()
    child.assert_called_once()


def test_scheduler_uses_deterministic_node_work_paths(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _node("bundle_a", "bundle")
    artifact = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    plan = _plan([bundle, artifact])
    observed: list[pathlib.Path] = []

    def fake(
        node: Mapping[str, Any], paths: ExecutionPaths
    ) -> tuple[pathlib.Path, ...]:
        observed.append(paths.work_dir)
        staged = paths.work_dir / "staged"
        staged.mkdir()
        return (staged,)

    monkeypatch.chdir(tmp_path)
    work_root = pathlib.Path("work")
    with mock.patch("ggbuild.scheduler.prepare_target"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=pathlib.Path("cache"),
                destination=pathlib.Path("output"),
                work_dir=work_root,
                node_ids=("bundle_a",),
                exact_node=True,
            ),
            config=fixture_config(tmp_path),
            executor=fake,
        )

    assert observed == [(work_root / "bundle_a").resolve()]
    assert observed[0].is_absolute()
    assert not observed[0].exists()


def test_exact_test_stops_at_downloaded_artifact_boundary(
    tmp_path: pathlib.Path,
) -> None:
    bundle = _node("bundle_a", "bundle")
    artifact = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    test = _node(
        "test_root",
        "test",
        dependencies=("artifact_root",),
        artifact_dependencies=("artifact_root",),
    )
    plan = _plan([bundle, artifact, test])
    calls: list[str] = []

    def fake(
        node: Mapping[str, Any], paths: ExecutionPaths
    ) -> tuple[pathlib.Path, ...]:
        calls.append(str(node["id"]))
        result = paths.output_dir / "test-results" / TARGET / "1.0.json"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text("{}", encoding="utf-8")
        return (result,)

    with mock.patch("ggbuild.scheduler.prepare_target"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                node_ids=("test_root",),
                exact_node=True,
            ),
            config=fixture_config(tmp_path),
            executor=fake,
        )

    assert calls == ["test_root"]


def _node(
    node_id: str,
    role: str,
    *,
    dependencies: tuple[str, ...] = (),
    artifact_dependencies: tuple[str, ...] = (),
) -> dict[str, Any]:
    inputs = {
        "build_options": BuildOptions().as_dict(),
        "id": node_id,
        "upstream": list(dependencies),
    }
    cache_key = "ggbuild-v3-" + digest_json(inputs)
    return {
        "bundle_install_subdir": "fixture",
        "artifact_dependencies": list(artifact_dependencies),
        "cache_key": cache_key,
        "direct_dependencies": list(dependencies),
        "expected_outputs": [
            {
                "format": "ggbuild-bundle-v2"
                if role == "bundle"
                else "ggbuild-artifact-v2",
                "path": (
                    f"bundles/{cache_key}.tar.zst"
                    if role == "bundle"
                    else f"artifacts/{TARGET}/1.0/"
                ),
            }
        ],
        "id": node_id,
        "inputs": inputs,
        "installation_path": f"install/{node_id}" if role == "bundle" else None,
        "package": node_id,
        "recipe": "tests.v2_recipe:Root",
        "role": role,
        "runtime_dependencies": [],
        "build_dependencies": [
            item for item in dependencies if item not in artifact_dependencies
        ],
        "target": TARGET,
        "version": "1.0",
    }


def _plan(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    nodes.sort(key=itemgetter("id"))
    graph = {node["id"]: node["direct_dependencies"] for node in nodes}
    root = next(
        (node for node in nodes if node["role"] == "test"),
        next(node for node in nodes if node["role"] == "artifact"),
    )
    closure = sorted(node["id"] for node in nodes)
    result = {
        "build_options": {},
        "format_version": PLAN_FORMAT_VERSION,
        "layers": topological_layers(graph),
        "nodes": nodes,
        "project": "fixture",
        "resolved_packages": {},
        "roots": [
            {
                "closure": closure,
                "closure_digest": digest_json([
                    next(
                        node["cache_key"]
                        for node in nodes
                        if node["id"] == item
                    )
                    for item in closure
                ]),
                "node_id": root["id"],
                "target": TARGET,
                "version": root["version"],
            }
        ],
        "targets": [TARGET],
    }
    validate_plan(result)
    return result


def test_bundle_round_trip_corruption_and_conflicts(
    tmp_path: pathlib.Path,
) -> None:
    node = _node("bundle_a", "bundle")
    source = tmp_path / "source"
    (source / "lib").mkdir(parents=True)
    (source / "share" / "terminfo").mkdir(parents=True)
    (source / "lib" / "library").write_bytes(b"library")
    (source / "lib" / "link").symlink_to("library")
    (source / "lib" / "terminfo").symlink_to("../share/terminfo")
    bundle = bundle_path(tmp_path, node["cache_key"])
    export_bundle(source, bundle, node=node)

    destination = tmp_path / "destination"
    restore_bundle(bundle, destination, node=node)
    assert (destination / "lib" / "library").read_bytes() == b"library"
    assert (destination / "lib" / "terminfo").readlink() == pathlib.Path(
        "../share/terminfo"
    )
    merge_bundle(bundle, tmp_path / "merged", node=node)
    merge_bundle(bundle, tmp_path / "merged", node=node)
    (tmp_path / "merged/lib/library").write_bytes(b"conflict")
    with pytest.raises(BundleError, match="conflicting dependency path"):
        merge_bundle(bundle, tmp_path / "merged", node=node)

    bundle.write_bytes(bundle.read_bytes()[:20])
    with pytest.raises(BundleError, match="corrupt"):
        restore_bundle(bundle, destination, node=node)


def test_bundle_export_excludes_unchanged_dependency_baseline(
    tmp_path: pathlib.Path,
) -> None:
    node = _node("bundle_b", "bundle")
    staging = tmp_path / "staging"
    (staging / "lib").mkdir(parents=True)
    (staging / "lib/dependency").write_text("a", encoding="utf-8")
    baseline = [
        {
            "mode": 0o755,
            "path": "lib",
            "type": "directory",
        },
        {
            "mode": 0o644,
            "path": "lib/dependency",
            "sha256": hashlib.sha256(b"a").hexdigest(),
            "size": 1,
            "type": "file",
        },
    ]
    (staging / "lib/dependency").write_text("changed", encoding="utf-8")
    (staging / "lib/package").write_text("b", encoding="utf-8")

    bundle = bundle_path(tmp_path, node["cache_key"])
    export_bundle(staging, bundle, node=node, baseline=baseline)
    restored = tmp_path / "restored"
    restore_bundle(bundle, restored, node=node)

    assert not (restored / "lib/dependency").exists()
    assert (restored / "lib/package").read_text(encoding="utf-8") == "b"


def test_bundle_rejects_symlink_that_escapes_tree(
    tmp_path: pathlib.Path,
) -> None:
    node = _node("bundle_a", "bundle")
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape").symlink_to("../outside")
    bundle = bundle_path(tmp_path, node["cache_key"])
    export_bundle(source, bundle, node=node)

    with pytest.raises(BundleError, match="unsafe symlink target"):
        restore_bundle(bundle, tmp_path / "destination", node=node)


def test_plan_rejects_v1_and_stale_cache_keys() -> None:
    plan = _plan([_node("artifact_root", "artifact")])
    with pytest.raises(ValueError, match="only v3"):
        validate_plan({**plan, "format_version": 1})
    plan["nodes"][0]["cache_key"] = "ggbuild-v3-" + "0" * 64
    with pytest.raises(ValueError, match="stale cache key"):
        validate_plan(plan)


def test_scheduler_bounds_parallelism_and_reuses_exact_bundles(
    tmp_path: pathlib.Path,
) -> None:
    bundles = [_node("bundle_a", "bundle"), _node("bundle_b", "bundle")]
    root = _node(
        "artifact_root",
        "artifact",
        dependencies=("bundle_a", "bundle_b"),
    )
    plan = _plan([*bundles, root])
    config = fixture_config(tmp_path)
    active = 0
    maximum = 0
    lock = threading.Lock()
    bundle_barrier = threading.Barrier(len(bundles))
    calls: list[str] = []

    def fake(node: Mapping[str, Any], paths: Any) -> tuple[pathlib.Path, ...]:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(node["id"])
        if node["role"] == "bundle":
            bundle_barrier.wait(timeout=5)
        with lock:
            active -= 1
        if node["role"] == "bundle":
            staged = paths.work_dir / "staged"
            staged.mkdir()
            (staged / node["id"]).write_text(node["id"], encoding="utf-8")
            return (staged,)
        output = paths.output_dir / TARGET / "1.0"
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / "root.tar.zst"
        artifact.write_text("root", encoding="utf-8")
        return (artifact,)

    options = RunOptions(
        cache_dir=tmp_path / "cache",
        destination=tmp_path / "output",
        max_parallel=2,
    )
    with mock.patch("ggbuild.scheduler.prepare_target"):
        first = run_plan(plan, options, config=config, executor=fake)
        calls.clear()
        second = run_plan(plan, options, config=config, executor=fake)

    assert maximum == 2
    assert len(first.cache_misses) == 2
    assert len(second.cache_hits) == 2
    assert calls == ["artifact_root"]


def test_node_cache_parser_accepts_repeated_and_comma_values() -> None:
    assert parse_node_cache((
        "tests.recipe:A=ignore,tests.recipe:B=reuse",
        "tests.recipe:C=ignore",
    )) == {
        "tests.recipe:A": "ignore",
        "tests.recipe:B": "reuse",
        "tests.recipe:C": "ignore",
    }

    assert parse_node_cache(("*=ignore,tests.recipe:A=reuse",)) == {
        "*": "ignore",
        "tests.recipe:A": "reuse",
    }
    assert parse_node_cache(("*=auto",)) == {"*": "auto"}
    assert parse_node_cache((
        "tests.recipe:A=ignore,tests.recipe:B=ignore",
        "tests.recipe:A=reuse",
    )) == {
        "tests.recipe:B": "ignore",
        "tests.recipe:A": "reuse",
    }
    for invalid in (
        "*foo=reuse",
        "foo*bar=reuse",
        "foo?=auto",
        "foo[ab]=ignore",
    ):
        with pytest.raises(ValueError, match="trailing"):
            parse_node_cache((invalid,))


def test_node_cache_uses_last_matching_rule() -> None:
    policies = parse_node_cache((
        "*=auto,tests.*=reuse,tests.postgres*=ignore",
        "tests.postgresql:PostgreSQL=reuse",
    ))
    assert policy_for(policies, ("other",)) == "auto"
    assert policy_for(policies, ("tests.recipe:A",)) == "reuse"
    assert policy_for(policies, ("tests.postgres17:PostgreSQL",)) == "ignore"
    assert policy_for(policies, ("tests.postgresql:PostgreSQL",)) == "reuse"
    reversed_policies = parse_node_cache(("tests.recipe:A=reuse,*=ignore",))
    assert policy_for(reversed_policies, ("tests.recipe:A",)) == "ignore"


def test_node_cache_ignore_rebuilds_exact_cached_bundle(
    tmp_path: pathlib.Path,
) -> None:
    bundle = _node("bundle_a", "bundle")
    bundle["recipe"] = "tests.recipe:A"
    root = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    plan = _plan([bundle, root])
    config = fixture_config(tmp_path)
    calls: list[str] = []

    def fake(node: Mapping[str, Any], paths: Any) -> tuple[pathlib.Path, ...]:
        calls.append(node["id"])
        if node["role"] == "bundle":
            staged = paths.work_dir / "staged"
            staged.mkdir()
            (staged / "value").write_text("new", encoding="utf-8")
            return (staged,)
        output = paths.output_dir / TARGET / "1.0"
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / "root.tar.zst"
        artifact.write_text("root", encoding="utf-8")
        return (artifact,)

    with mock.patch("ggbuild.scheduler.prepare_target"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
            ),
            config=config,
            executor=fake,
        )
        calls.clear()
        result = run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                node_cache=("tests.recipe:A=ignore",),
            ),
            config=config,
            executor=fake,
        )

    assert result.cache_misses == ["bundle_a"]
    assert calls == ["bundle_a", "artifact_root"]


def test_node_cache_wildcard_allows_specific_reuse_override(
    tmp_path: pathlib.Path,
) -> None:
    bundle_a = _node("bundle_a", "bundle")
    bundle_b = _node("bundle_b", "bundle")
    bundle_a["recipe"] = "tests.recipe:A"
    bundle_b["recipe"] = "tests.recipe:B"
    root = _node(
        "artifact_root",
        "artifact",
        dependencies=("bundle_a", "bundle_b"),
    )
    plan = _plan([bundle_a, bundle_b, root])
    config = fixture_config(tmp_path)
    calls: list[str] = []

    def fake(node: Mapping[str, Any], paths: Any) -> tuple[pathlib.Path, ...]:
        calls.append(node["id"])
        if node["role"] == "bundle":
            staged = paths.work_dir / "staged"
            staged.mkdir()
            (staged / node["id"]).write_text(node["id"], encoding="utf-8")
            return (staged,)
        output = paths.output_dir / TARGET / "1.0"
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / "root.tar.zst"
        artifact.write_text("root", encoding="utf-8")
        return (artifact,)

    with mock.patch("ggbuild.scheduler.prepare_target"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
            ),
            config=config,
            executor=fake,
        )
        calls.clear()
        result = run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                node_cache=("*=ignore,tests.recipe:B=reuse",),
            ),
            config=config,
            executor=fake,
        )

    assert result.cache_hits == ["bundle_b"]
    assert result.cache_misses == ["bundle_a"]
    assert calls == ["bundle_a", "artifact_root"]


def test_node_cache_reuse_accepts_compatible_prior_content_key(
    tmp_path: pathlib.Path,
) -> None:
    previous = _node("bundle_a", "bundle")
    previous["recipe"] = "tests.recipe:A"
    source = tmp_path / "source"
    source.mkdir()
    (source / "value").write_text("cached", encoding="utf-8")
    old_bundle = bundle_path(tmp_path / "cache", previous["cache_key"])
    export_bundle(source, old_bundle, node=previous)

    current = _node("bundle_a", "bundle")
    current["recipe"] = "tests.recipe:A"
    current["inputs"] = {**current["inputs"], "revision": "new"}
    current["cache_key"] = "ggbuild-v3-" + digest_json(current["inputs"])
    current["expected_outputs"][0]["path"] = (
        f"bundles/{current['cache_key']}.tar.zst"
    )
    root = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    root["inputs"] = {
        **root["inputs"],
        "upstream_key": current["cache_key"],
    }
    root["cache_key"] = "ggbuild-v3-" + digest_json(root["inputs"])
    plan = _plan([current, root])
    calls: list[str] = []

    def fake(node: Mapping[str, Any], paths: Any) -> tuple[pathlib.Path, ...]:
        calls.append(node["id"])
        output = paths.output_dir / TARGET / "1.0"
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / "root.tar.zst"
        artifact.write_text("root", encoding="utf-8")
        return (artifact,)

    with mock.patch("ggbuild.scheduler.prepare_target"):
        result = run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                node_cache=("tests.recipe:A=reuse",),
            ),
            config=fixture_config(tmp_path),
            executor=fake,
        )

    assert result.cache_hits == ["bundle_a"]
    assert calls == ["artifact_root"]
    assert bundle_path(tmp_path / "cache", current["cache_key"]).is_file()


def test_node_cache_rejects_invalid_selection_and_role(
    tmp_path: pathlib.Path,
) -> None:
    bundle = _node("bundle_a", "bundle")
    bundle["recipe"] = "tests.recipe:A"
    root = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    plan = _plan([bundle, root])
    with pytest.raises(ValueError, match="outside the selected closure"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                dry_run=True,
                node_cache=("unknown=ignore",),
            ),
            config=fixture_config(tmp_path),
        )
    with pytest.raises(ValueError, match="only applies to bundle nodes"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                dry_run=True,
                node_cache=("tests.v2_recipe:Root=ignore",),
            ),
            config=fixture_config(tmp_path),
        )
    result = run_plan(
        plan,
        RunOptions(
            cache_dir=tmp_path / "cache",
            destination=tmp_path / "output",
            dry_run=True,
            node_cache=("tests.*=reuse,tests.v2_recipe:Root=auto",),
        ),
        config=fixture_config(tmp_path),
    )
    assert result.cache_misses == ["bundle_a"]
    with pytest.raises(ValueError, match="conflicts"):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                dry_run=True,
                no_cache=True,
                node_cache=("tests.recipe:A=reuse",),
            ),
            config=fixture_config(tmp_path),
        )


def test_node_cache_reuse_rebuilds_without_compatible_bundle(
    tmp_path: pathlib.Path,
) -> None:
    other = _node("bundle_other", "bundle")
    source = tmp_path / "source"
    source.mkdir()
    (source / "value").write_text("other", encoding="utf-8")
    export_bundle(
        source,
        bundle_path(tmp_path / "cache", other["cache_key"]),
        node=other,
    )
    bundle = _node("bundle_a", "bundle")
    bundle["recipe"] = "tests.recipe:A"
    root = _node("artifact_root", "artifact", dependencies=("bundle_a",))
    plan = _plan([bundle, root])
    calls: list[str] = []

    def fake(node: Mapping[str, Any], paths: Any) -> tuple[pathlib.Path, ...]:
        calls.append(node["id"])
        if node["role"] == "bundle":
            staged = paths.work_dir / "staged"
            staged.mkdir()
            (staged / "value").write_text("rebuilt", encoding="utf-8")
            return (staged,)
        output = paths.output_dir / TARGET / "1.0"
        output.mkdir(parents=True, exist_ok=True)
        artifact = output / "root.tar.zst"
        artifact.write_text("root", encoding="utf-8")
        return (artifact,)

    with mock.patch("ggbuild.scheduler.prepare_target"):
        result = run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
                node_cache=("tests.recipe:A=reuse",),
            ),
            config=fixture_config(tmp_path),
            executor=fake,
        )

    assert result.cache_misses == ["bundle_a"]
    assert calls == ["bundle_a", "artifact_root"]


def test_failed_test_node_prevents_completion_and_is_never_cached(
    tmp_path: pathlib.Path,
) -> None:
    artifact_node = _node("artifact_root", "artifact")
    test_node = _node(
        "test_root",
        "test",
        dependencies=("artifact_root",),
        artifact_dependencies=("artifact_root",),
    )
    plan = _plan([artifact_node, test_node])
    calls: list[str] = []

    def failing(
        node: Mapping[str, Any], paths: Any
    ) -> tuple[pathlib.Path, ...]:
        calls.append(node["id"])
        if node["role"] == "artifact":
            output = paths.output_dir / TARGET / "1.0"
            output.mkdir(parents=True, exist_ok=True)
            artifact = output / "root.tar.zst"
            artifact.write_text("root", encoding="utf-8")
            return (artifact,)
        raise RuntimeError("test failed")

    with (
        mock.patch("ggbuild.scheduler.prepare_target"),
        pytest.raises(RuntimeError, match="test failed"),
    ):
        run_plan(
            plan,
            RunOptions(
                cache_dir=tmp_path / "cache",
                destination=tmp_path / "output",
            ),
            config=fixture_config(tmp_path),
            executor=failing,
        )
    assert calls == ["artifact_root", "test_root"]
    assert not list((tmp_path / "cache").glob("ggbuild-v3-*"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda test, _artifact: (
                test.__setitem__("direct_dependencies", []),
                test.__setitem__("artifact_dependencies", []),
            ),
            "invalid test dependencies",
        ),
        (
            lambda test, _artifact: (
                test.__setitem__("direct_dependencies", ["bundle_dependency"]),
                test.__setitem__(
                    "artifact_dependencies", ["bundle_dependency"]
                ),
            ),
            "invalid artifact dependencies",
        ),
        (
            lambda test, artifact: (
                artifact["direct_dependencies"].append(test["id"]),
                artifact["direct_dependencies"].sort(),
                artifact["artifact_dependencies"].append(test["id"]),
            ),
            "invalid artifact dependencies",
        ),
    ],
)
def test_plan_rejects_invalid_artifact_dependency_protocol(
    mutate: Any, message: str
) -> None:
    bundle = _node("bundle_dependency", "bundle")
    artifact = _node(
        "artifact_root", "artifact", dependencies=("bundle_dependency",)
    )
    test = _node(
        "test_root",
        "test",
        dependencies=("artifact_root",),
        artifact_dependencies=("artifact_root",),
    )
    plan = _plan([bundle, artifact, test])
    mutate(test, artifact)

    with pytest.raises(ValueError, match=message):
        validate_plan(plan)


def test_host_mismatch_is_rejected() -> None:
    target = TargetConfig(
        "aarch64-apple-darwin",
        "host",
        "macos-latest",
    )
    with pytest.raises(ValueError, match="matching host hardware"):
        validate_execution(target, host_system="Darwin", host_arch="x86_64")


def test_docker_target_allows_emulated_architecture() -> None:
    target = TargetConfig(
        "x86_64-unknown-linux-gnu",
        "docker",
        "ubuntu-latest",
    )

    validate_execution(target, host_system="Darwin", host_arch="aarch64")


def test_target_dockerfile_identity_tracks_inputs() -> None:
    environment = docker_environment(TARGET)

    rendered = environment.render_dockerfile()

    assert len(environment.template_sha256) == 64
    assert len(environment.dockerfile_sha256) == 64
    assert "sccache-v0.17.0-${sccache_arch}-unknown-linux-musl" in rendered
    assert "67c4a96dd237c1f5" in rendered
    assert "821a86343191aa1c" in rendered
    assert "/usr/share/licenses/sccache/LICENSE" in rendered
    with mock.patch(
        "ggbuild.targets.linux.dockerfile.importlib.metadata.requires",
        return_value=["dynamic-dependency>=1"],
    ):
        assert (
            environment.dockerfile_sha256
            != hashlib.sha256(rendered.encode()).hexdigest()
        )

    with pytest.raises(ValueError, match="unknown Docker template"):
        dockerfile_template("unknown")
    with pytest.raises(ValueError, match="requires variable 'uv_image'"):
        dockerfile_template("linux-musl").render(
            base_image="alpine:fixture", variables={}
        )


def test_registry_action_context_is_content_addressed(
    tmp_path: pathlib.Path,
) -> None:
    x86 = docker_environment("x86_64-unknown-linux-gnu")
    arm = docker_environment("aarch64-unknown-linux-gnu")

    assert x86.action_source_sha256 == arm.action_source_sha256
    assert x86.action_image == arm.action_image
    assert x86.action_image == (
        "ghcr.io/vercel-labs/ggbuild/linux-gnu:"
        f"sha256-{x86.action_source_sha256}"
    )
    rendered = x86.render_action_dockerfile()

    written = x86.write_action_context(tmp_path)
    assert written == (
        tmp_path / "Dockerfile",
        tmp_path / "action-entrypoint.sh",
    )
    assert written[0].read_text(encoding="utf-8") == rendered
    assert written[1].read_text(encoding="utf-8") == action_entrypoint()


@pytest.mark.parametrize("libc", ["gnu", "musl"])
def test_runtime_test_context_is_minimal_content_addressed_and_licensed(
    tmp_path: pathlib.Path,
    libc: str,
) -> None:
    target = f"x86_64-unknown-linux-{libc}"
    rendered = bare_test_dockerfile(target)
    image = bare_test_image(target)

    assert "FROM alpine:3.16.2@sha256:" in rendered
    if libc == "gnu":
        assert "FROM rockylinux:8@sha256:" in rendered
        assert "rpm -ql glibc libgcc" in rendered
    else:
        assert "apk info -L musl" in rendered
        assert "musl-1.2.3.tar.gz" in rendered
    assert "BUSYBOX-GPL-2.0-only" in rendered
    assert "busybox-static=1.35.0-r18" in rendered
    assert "busybox.net/downloads/busybox-1.35.0.tar.bz2" in rendered
    assert "tar -xOf /tmp/busybox.tar.bz2" in rendered
    assert "FROM scratch" in rendered
    assert "COPY --from=rootfs /rootfs /" in rendered
    assert "for applet in sh dirname mkdir rmdir diff" in rendered
    assert "busybox --install" not in rendered
    assert image == (
        f"ghcr.io/vercel-labs/ggbuild/test-linux-{libc}:"
        f"sha256-{bare_test_source_sha256(target)}"
    )
    path = write_bare_test_context(tmp_path, target)
    assert path.read_text(encoding="utf-8") == rendered


@pytest.mark.parametrize(
    ("target", "name", "platform"),
    [
        ("x86_64-unknown-linux-gnu", "linux-gnu", "linux/amd64"),
        ("aarch64-unknown-linux-gnu", "linux-gnu", "linux/arm64"),
        ("x86_64-unknown-linux-musl", "linux-musl", "linux/amd64"),
        ("aarch64-unknown-linux-musl", "linux-musl", "linux/arm64"),
    ],
)
def test_linux_docker_environment_table(
    target: str, name: str, platform: str
) -> None:
    environment = docker_environment(target)

    assert environment.target == target
    assert environment.name == name
    assert environment.platform == platform
    assert "@sha256:" in environment.image


def test_target_policy_defaults_and_project_overrides(
    tmp_path: pathlib.Path,
) -> None:
    linux = target_policy("aarch64-unknown-linux-musl")
    macos = target_policy("x86_64-apple-darwin")
    assert (linux.execution, linux.runner) == (
        "docker",
        "ubuntu-24.04-arm",
    )
    assert (macos.execution, macos.runner, dict(macos.environment)) == (
        "host",
        "macos-15-intel",
        {"MACOSX_DEPLOYMENT_TARGET": "13.0"},
    )

    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "fixture"

[tool.ggbuild]
root-recipe = "tests.v2_recipe:Root"
bundle-prefix = "fixture"

[[tool.ggbuild.target]]
triple = "x86_64-unknown-linux-gnu"
execution = "host"

[[tool.ggbuild.target]]
triple = "x86_64-apple-darwin"
runner = "custom-macos-runner"
environment = { MACOSX_DEPLOYMENT_TARGET = "14.0", CUSTOM = "yes" }
""".lstrip(),
        encoding="utf-8",
    )
    config = load_project(tmp_path)
    linux_config, macos_config = config.targets
    assert (linux_config.execution, linux_config.runner) == (
        "host",
        "ubuntu-latest",
    )
    assert (macos_config.execution, macos_config.runner) == (
        "host",
        "custom-macos-runner",
    )
    assert macos_config.environment_dict == {
        "CUSTOM": "yes",
        "MACOSX_DEPLOYMENT_TARGET": "14.0",
    }
    assert config.sccache == SccacheConfig()
    assert config.workflow.max_concurrency == 12

    with pytest.raises(ValueError, match="no built-in Docker environment"):
        docker_environment("aarch64-apple-darwin")


def test_workflow_max_concurrency_configuration(
    tmp_path: pathlib.Path,
) -> None:
    project = """
[project]
name = "fixture"

[tool.ggbuild]
root-recipe = "tests.v2_recipe:Root"
bundle-prefix = "fixture"

[[tool.ggbuild.target]]
triple = "x86_64-unknown-linux-gnu"

[tool.ggbuild.workflow]
max-concurrency = {value}
"""
    (tmp_path / "pyproject.toml").write_text(
        project.format(value=7), encoding="utf-8"
    )
    assert load_project(tmp_path).workflow.max_concurrency == 7

    (tmp_path / "pyproject.toml").write_text(
        project.format(value=0), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="max-concurrency must be positive"):
        load_project(tmp_path)


def test_build_dbgsym_configuration(
    tmp_path: pathlib.Path,
) -> None:
    project = """
[project]
name = "fixture"

[tool.ggbuild]
root-recipe = "tests.v2_recipe:Root"
bundle-prefix = "fixture"

[[tool.ggbuild.target]]
triple = "x86_64-unknown-linux-gnu"

[tool.ggbuild.build]
{setting} = true
"""
    (tmp_path / "pyproject.toml").write_text(
        project.format(setting="build-dbgsym"), encoding="utf-8"
    )
    assert load_project(tmp_path).build_options.build_dbgsym is True


def test_publication_configuration(tmp_path: pathlib.Path) -> None:
    project = """
[project]
name = "fixture"

[tool.ggbuild]
root-recipe = "tests.v2_recipe:Root"
bundle-prefix = "fixture"

[[tool.ggbuild.target]]
triple = "x86_64-unknown-linux-gnu"

[tool.ggbuild.publication]
repository = "example/project"
{index_url}
{bypass_setting}
"""
    (tmp_path / "pyproject.toml").write_text(
        project.format(index_url="", bypass_setting=""),
        encoding="utf-8",
    )
    assert load_project(tmp_path).publication == PublicationConfig(
        "example/project"
    )
    (tmp_path / "pyproject.toml").write_text(
        project.format(
            index_url='index-url = "https://example.test/ingest"',
            bypass_setting=('protection-bypass-secret = "VERCEL_BYPASS"'),
        ),
        encoding="utf-8",
    )
    assert load_project(tmp_path).publication == PublicationConfig(
        "example/project", "https://example.test/ingest", "VERCEL_BYPASS"
    )
    (tmp_path / "pyproject.toml").write_text(
        project.format(
            index_url='index-url = "http://example.test/ingest"',
            bypass_setting="",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="HTTPS URL"):
        load_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        project.replace('repository = "example/project"', "").format(
            index_url="", bypass_setting=""
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="owner/name"):
        load_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        project.format(
            index_url='index-url = "https://example.test/ingest"',
            bypass_setting=('protection-bypass-secret = "invalid-secret"'),
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="GitHub Actions secret name"):
        load_project(tmp_path)


def test_target_preparation_builds_rendered_dockerfile_for_platform(
    tmp_path: pathlib.Path,
) -> None:
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(
            TargetConfig(
                TARGET,
                "docker",
                "ubuntu-latest",
            ),
        ),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )

    with (
        mock.patch("ggbuild.execution.validate_execution"),
        mock.patch("ggbuild.execution.run_docker_build") as run,
    ):
        prepare_target(config, TARGET)

    assert (
        run.call_args.kwargs["dockerfile"]
        == docker_environment(TARGET).render_dockerfile()
    )
    assert run.call_args.kwargs["image"] == "ggbuild/fixture:linux-gnu-x86_64"
    assert run.call_args.kwargs["platform"] == "linux/amd64"


def test_docker_build_uses_sdk_tar_context() -> None:
    client = mock.MagicMock()
    context_contents = b""

    def build(**options: Any) -> Any:
        nonlocal context_contents
        context_contents = options["fileobj"].read()
        return iter([{"stream": "#1 DONE 0.1s\n"}])

    client.api.build.side_effect = build

    with mock.patch(
        "ggbuild.execution.docker.DockerClient.from_env",
        return_value=client,
    ) as from_env:
        run_docker_build(
            dockerfile="FROM scratch\n",
            environment={"DOCKER_HOST": "unix:///docker.sock"},
            image="ggbuild/fixture:test",
            platform="linux/amd64",
        )

    from_env.assert_called_once_with(
        environment={"DOCKER_HOST": "unix:///docker.sock"}
    )
    options = client.api.build.call_args.kwargs
    assert options["custom_context"] is True
    assert options["dockerfile"] == "Dockerfile"
    assert options["tag"] == "ggbuild/fixture:test"
    assert options["platform"] == "linux/amd64"
    with tarfile.open(
        fileobj=io.BytesIO(context_contents), mode="r:"
    ) as archive:
        member = archive.extractfile("Dockerfile")
        assert member is not None
        assert member.read() == b"FROM scratch\n"
    client.close.assert_called_once_with()


def test_docker_user_is_always_non_root() -> None:
    client = mock.MagicMock()
    client.info.return_value = {"SecurityOptions": ["name=rootless"]}
    assert all(
        int(part) != 0
        for part in execution.docker_container_user(client).split(":")
    )
    with (
        mock.patch(
            "ggbuild.execution.os.getuid", return_value=1000, create=True
        ),
        mock.patch(
            "ggbuild.execution.os.getgid", return_value=1000, create=True
        ),
    ):
        assert execution.docker_container_user(client) == "1000:1000"
    with (
        mock.patch("ggbuild.execution.os.getuid", return_value=0, create=True),
        mock.patch("ggbuild.execution.os.getgid", return_value=0, create=True),
    ):
        user = execution.docker_container_user(client)
        assert all(int(part) != 0 for part in user.split(":"))


def test_docker_container_uses_sdk_logs_and_removes_container(
    tmp_path: pathlib.Path,
) -> None:
    user = "1000:1000"
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "docker", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "bundles",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    environment = {
        "GGBUILD_BUNDLE_INSTALL_SUBDIR": "fixture",
        "GGBUILD_CONTAINER_CACHE": str(tmp_path / "cache"),
        "GGBUILD_NODE_STAGING": str(tmp_path / "work" / "sysroot"),
        "GGBUILD_PREBUILT_PACKAGES": "[]",
        "GGBUILD_ENABLE_SCCACHE": "1",
        "ACTIONS_RESULTS_URL": "https://cache.invalid/",
        "ACTIONS_RUNTIME_TOKEN": "fixture",
        "SCCACHE_GHA_ENABLED": "on",
        "ACTIONS_CACHE_SERVICE_V2": "on",
    }
    client = mock.MagicMock()
    container = client.containers.create.return_value
    logs = iter([b'{"message":"building"}\n'])
    container.logs.return_value = logs
    container.wait.return_value = {"StatusCode": 0}

    with (
        mock.patch(
            "ggbuild.execution.docker.DockerClient.from_env",
            return_value=client,
        ),
        mock.patch(
            "ggbuild.execution.docker_container_user", return_value=user
        ),
        mock.patch("ggbuild.execution.DockerLogSource") as log_source,
        mock.patch("ggbuild.execution.tail_to_status") as tail,
        mock.patch("ggbuild.execution.status"),
    ):
        drained = tail.return_value.__enter__.return_value
        drained.error = None
        run_docker_container(
            _node("bundle_a", "bundle"),
            paths,
            config,
            raw_output=tmp_path / "work" / "raw-output",
            environment=environment,
            subject="fixture",
        )

    options = client.containers.create.call_args.kwargs
    assert options["image"] == "ggbuild/fixture:linux-gnu-x86_64"
    assert options["platform"] == "linux/amd64"
    assert options["init"] is True
    assert options["entrypoint"] == "/opt/venv/bin/python"
    assert "stdout" not in options
    assert "stderr" not in options
    assert options["user"] == user
    assert options["environment"]["HOME"] == "/ggbuild-node/home"
    assert options["environment"]["TMPDIR"] == "/ggbuild-node/tmp"
    assert (tmp_path / "work" / "tmp").is_dir()
    assert options["environment"]["LC_ALL"] == "C.UTF-8"
    assert {mount["bind"] for mount in options["volumes"].values()} >= {
        "/etc/passwd",
        "/etc/group",
    }
    identity = tmp_path / "bundles" / "container-identity" / "passwd-1000-1000"
    assert identity.read_text() == (
        "root:x:0:0:root:/root:/bin/sh\n"
        "ggbuild:x:1000:1000:ggbuild:"
        "/ggbuild-node/home:/bin/sh\n"
    )
    assert options["command"][:3] == ["-m", "ggbuild", "build"]
    assert options["volumes"][str(tmp_path / "work" / "raw-output")] == {
        "bind": "/artifacts",
        "mode": "rw",
    }
    assert all(
        mount["mode"] == "ro"
        for mount in options["volumes"].values()
        if "/opt/ggbuild-modules" in mount["bind"]
    )
    assert options["environment"]["LOGRAIL_OUTPUT"] == "plain"
    assert (
        options["environment"]["ACTIONS_RUNTIME_TOKEN"]
        == environment["ACTIONS_RUNTIME_TOKEN"]
    )
    assert options["environment"]["GGBUILD_ENABLE_SCCACHE"] == "1"
    assert "--enable-sccache" in options["command"]
    assert "--work-dir=/ggbuild-node/build" in options["command"]
    log_source.assert_called_once_with(logs)
    container.start.assert_called_once_with()
    container.remove.assert_called_once_with(force=True)
    client.close.assert_called_once_with()


def test_docker_container_requires_non_root_user(
    tmp_path: pathlib.Path,
) -> None:
    user = "1000:1000"
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "docker", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "bundles",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    environment = {
        "GGBUILD_BUNDLE_INSTALL_SUBDIR": "fixture",
        "GGBUILD_NODE_STAGING": str(tmp_path / "work" / "sysroot"),
        "GGBUILD_PREBUILT_PACKAGES": "[]",
    }
    client = mock.MagicMock()
    container = client.containers.create.return_value
    container.logs.return_value = iter(())
    container.wait.return_value = {"StatusCode": 0}

    with (
        mock.patch(
            "ggbuild.execution.docker.DockerClient.from_env",
            return_value=client,
        ),
        mock.patch(
            "ggbuild.execution.docker_container_user", return_value=user
        ) as container_user,
        mock.patch("ggbuild.execution.DockerLogSource"),
        mock.patch("ggbuild.execution.tail_to_status") as tail,
        mock.patch("ggbuild.execution.status"),
    ):
        tail.return_value.__enter__.return_value.error = None
        run_docker_container(
            _node("test_a", "test"),
            paths,
            config,
            raw_output=tmp_path / "work" / "raw-output",
            environment=environment,
            subject="fixture tests",
        )

    container_user.assert_called_once_with(client)
    options = client.containers.create.call_args.kwargs
    assert options["user"] == user
    assert options["environment"]["TMPDIR"] == str(
        pathlib.PurePosixPath("/") / "tmp"
    )
    assert "--work-dir=/ggbuild-node/test-work" in options["command"]


def test_docker_container_removes_container_when_start_fails(
    tmp_path: pathlib.Path,
) -> None:
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "docker", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "bundles",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    environment = {
        "GGBUILD_BUNDLE_INSTALL_SUBDIR": "fixture",
        "GGBUILD_CONTAINER_CACHE": str(tmp_path / "cache"),
        "GGBUILD_NODE_STAGING": str(tmp_path / "work" / "sysroot"),
        "GGBUILD_PREBUILT_PACKAGES": "[]",
    }
    client = mock.MagicMock()
    container = client.containers.create.return_value
    container.start.side_effect = RuntimeError("start failed")

    with (
        mock.patch(
            "ggbuild.execution.docker.DockerClient.from_env",
            return_value=client,
        ),
        mock.patch(
            "ggbuild.execution.docker_container_user", return_value=None
        ),
        pytest.raises(RuntimeError, match="start failed"),
    ):
        run_docker_container(
            _node("bundle_a", "bundle"),
            paths,
            config,
            raw_output=tmp_path / "work" / "raw-output",
            environment=environment,
            subject="fixture",
        )

    container.remove.assert_called_once_with(force=True)
    client.close.assert_called_once_with()


def test_docker_container_nonzero_exit_still_removes_container(
    tmp_path: pathlib.Path,
) -> None:
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "docker", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "bundles",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    environment = {
        "GGBUILD_BUNDLE_INSTALL_SUBDIR": "fixture",
        "GGBUILD_NODE_STAGING": str(tmp_path / "work" / "sysroot"),
        "GGBUILD_PREBUILT_PACKAGES": "[]",
    }
    client = mock.MagicMock()
    container = client.containers.create.return_value
    container.logs.return_value = iter(())
    container.wait.return_value = {"StatusCode": 17}

    with (
        mock.patch(
            "ggbuild.execution.docker.DockerClient.from_env",
            return_value=client,
        ),
        pytest.raises(Exception, match="17"),
    ):
        run_docker_container(
            _node("bundle_a", "bundle"),
            paths,
            config,
            raw_output=tmp_path / "work" / "raw-output",
            environment=environment,
            subject="fixture",
        )

    container.remove.assert_called_once_with(force=True)
    client.close.assert_called_once_with()


def test_node_environment_honors_container_cache_override(
    tmp_path: pathlib.Path,
) -> None:
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "docker", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "bundles",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    override = tmp_path / "linux-cache"

    with mock.patch.dict(
        "os.environ", {"GGBUILD_CONTAINER_CACHE": str(override)}, clear=True
    ):
        environment = node_environment(
            _node("bundle_a", "bundle"), paths, config
        )

    assert environment["GGBUILD_CONTAINER_CACHE"] == str(override)


def test_prepared_target_executes_natively_and_requires_exact_host(
    tmp_path: pathlib.Path,
) -> None:
    config = ProjectConfig(
        root=tmp_path,
        project_name="fixture",
        root_recipe="tests.v2_recipe:Root",
        release_selection="latest-per-major",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "docker", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )
    node = _node("artifact_root", "artifact")
    paths = ExecutionPaths(
        bundle_dir=tmp_path / "bundles",
        install_dir=tmp_path / "install",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )

    command = build_command(
        node,
        paths,
        config,
        environment={},
        prepared_target=TARGET,
    )
    assert command[:3] == [command[0], "-m", "ggbuild"]
    assert command[3] == "build"
    assert f"--work-dir={paths.work_dir / 'build'}" in command
    assert "docker" not in command
    with pytest.raises(ValueError, match="does not match prepared target"):
        build_command(
            node,
            paths,
            config,
            environment={},
            prepared_target="aarch64-unknown-linux-gnu",
        )

    validate_prepared_target(
        config.target(TARGET),
        host_system="Linux",
        host_arch="x86_64",
        host_libc="glibc",
    )
    with pytest.raises(ValueError, match="exact host match"):
        validate_prepared_target(
            config.target(TARGET),
            host_system="Linux",
            host_arch="x86_64",
            host_libc="musl",
        )


def test_updater_selects_stable_versions_and_writes_atomically(
    tmp_path: pathlib.Path,
) -> None:
    with mock.patch(
        "ggbuild.updater.fetch",
        return_value=b"pkg-1.0.tar.xz pkg-1.2rc1.tar.xz pkg-1.1.tar.xz",
    ):
        assert (
            latest_version({
                "type": "html-index",
                "url": "https://example.com/",
                "pattern": r"pkg-([0-9a-z.]+)\.tar\.xz",
            })
            == "1.1"
        )

    destination = tmp_path / "release.py"
    destination.write_text("old\n", encoding="utf-8")
    write_atomic(destination, "new\n")
    assert destination.read_text(encoding="utf-8") == "new\n"

    with (
        mock.patch.object(pathlib.Path, "replace", side_effect=OSError("boom")),
        pytest.raises(OSError, match="boom"),
    ):
        write_atomic(destination, "partial\n")
    assert destination.read_text(encoding="utf-8") == "new\n"


def test_updater_rewrites_complete_release_set(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "recipe.py"
    destination.write_text(
        'class Fixture:\n    pass\n\nFixture(\n    "1.0",\n'
        f'    sha256="{"a" * 64}",\n)\n\nFixture(\n    "2.0",\n'
        f'    sha256="{"b" * 64}",\n)\n',
        encoding="utf-8",
    )
    texts: dict[pathlib.Path, str] = {}
    monkeypatch.setattr(
        "ggbuild.updater.inspect.getsourcefile",
        lambda _recipe: str(destination),
    )

    rewrite_declarations(
        cast("type[ggbuild_packages.BundledPackage]", type("Fixture", (), {})),
        {"2.1": "c" * 64, "3.0": "d" * 64},
        texts,
    )

    assert '"1.0"' not in texts[destination]
    assert '"2.0"' not in texts[destination]
    assert texts[destination].index('"2.1"') < texts[destination].index('"3.0"')


def test_multi_file_write_rolls_back_on_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("old first", encoding="utf-8")
    second.write_text("old second", encoding="utf-8")
    real_write = write_atomic

    def fail_second(path: pathlib.Path, text: str) -> None:
        if path == second:
            raise OSError("boom")
        real_write(path, text)

    monkeypatch.setattr("ggbuild.updater.write_atomic", fail_second)
    with pytest.raises(OSError, match="boom"):
        write_transaction({first: "new first", second: "new second"})

    assert first.read_text(encoding="utf-8") == "old first"
    assert second.read_text(encoding="utf-8") == "old second"


def test_update_check_only_discovers_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Fixture(UpdateableBundledCAutoconfPackage):
        title, ident = "Fixture", "update-check-fixture"
        sources: ClassVar[list[str | package_sources.SourceDecl]] = [
            {"url": "https://example.com/fixture-{version}.tar.gz"}
        ]
        update_policy: ClassVar[UpdatePolicy] = {
            "type": "html-index",
            "url": "https://example.com/",
            "pattern": r"fixture-(.+)\.tar\.gz",
        }

        @classmethod
        def discover_releases(cls) -> tuple[str, ...]:
            return ("2.0",)

    package = Fixture("1.0", sha256="a" * 64)
    monkeypatch.setattr(
        "ggbuild.updater.registered_releases", lambda: (package,)
    )
    reroll = mock.Mock()
    monkeypatch.setattr(
        "ggbuild.updater.package_patches.reroll_patches", reroll
    )

    with pytest.raises(ValueError, match=r"1\.0 -> 2\.0"):
        update_releases(check=True)

    reroll.assert_not_called()
