# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Static GitHub Actions workflow mirroring the canonical v3 plan DAG."""

from __future__ import annotations

from typing import Any

import json
import os
import pathlib
import re

from ggbuild.ci_protocol import digest_json
from ggbuild.dist import get_origin_commit_id
from ggbuild.planner import create_plan
from ggbuild.project import ProjectConfig, load_project
from ggbuild.targets.linux.dockerfile import bare_test_image, docker_environment

_CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
_CACHE = "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
_DOWNLOAD = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
_UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
_LOGIN = "docker/login-action@dbcb813823bdd20940b903addbd779551569679f"
_GITHUB_SCRIPT = (
    "actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3"
)


def ggbuild_source_revision() -> str:
    """Return the Git revision recorded for the ggbuild installation."""
    revision = get_origin_commit_id("ggbuild")
    if revision is None or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError(
            "registry-backed Linux Actions require ggbuild to be installed "
            "from a full Git commit"
        )
    return revision


def _bundle_artifact(node: dict[str, Any]) -> str:
    digest = str(node["cache_key"]).removeprefix("ggbuild-v3-")
    return f"ggbuild-bundle-{node['target']}-{digest}"


def _closure(nodes: dict[str, dict[str, Any]], node_id: str) -> set[str]:
    result: set[str] = set()

    def add(current: str) -> None:
        if current in result:
            return
        result.add(current)
        for dependency in nodes[current]["direct_dependencies"]:
            add(str(dependency))

    add(node_id)
    return result


def _artifact_handoff(node: dict[str, Any]) -> str:
    digest = str(node["cache_key"]).removeprefix("ggbuild-v3-")
    return f"ggbuild-root-{node['target']}-{digest}"


def _matrix_yaml(entries: list[dict[str, str | bool]]) -> str:
    lines: list[str] = []
    for entry in entries:
        first_name, first_value = next(iter(entry.items()))
        lines.append(f"          - {first_name}: {json.dumps(first_value)}")
        for name, value in list(entry.items())[1:]:
            lines.append(f"            {name}: {json.dumps(value)}")
    return "\n".join(lines)


def _node_matrix_entry(
    config: ProjectConfig,
    nodes: dict[str, dict[str, Any]],
    node_id: str,
    uploads: dict[str, tuple[str, bool]],
    test_inputs: dict[str, str],
) -> dict[str, str | bool]:
    node = nodes[node_id]
    target = str(node["target"])
    target_config = config.target(target)
    role = str(node["role"])
    upload_name, temporary = uploads.get(node_id, ("", False))
    has_bundles = role != "test" and any(
        nodes[dependency]["role"] == "bundle"
        for dependency in _closure(nodes, node_id) - {node_id}
    )
    return {
        "node": node_id,
        "target": target,
        "runner": target_config.runner,
        "execution": target_config.execution,
        "role": role,
        "package": str(node["package"]),
        "version": str(node["version"]),
        "cache_key": str(node["cache_key"]),
        "bundle_artifact": (_bundle_artifact(node) if role == "bundle" else ""),
        "has_bundles": has_bundles,
        "bare_linux_test": role == "test" and "-linux-" in target,
        "build_image": (
            docker_environment(target).action_image
            if target_config.execution == "docker"
            else ""
        ),
        "test_image": (
            bare_test_image(target)
            if role == "test" and "-linux-" in target
            else ""
        ),
        "input_artifact": test_inputs.get(node_id, ""),
        "upload_name": upload_name,
        "temporary_upload": temporary,
        "publish_root": role == "test" and config.publication is not None,
    }


def _job_component(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _node_matrices(
    config: ProjectConfig, plan: dict[str, Any]
) -> list[tuple[str, tuple[str, ...], list[dict[str, str | bool]]]]:
    nodes = {str(node["id"]): node for node in plan["nodes"]}
    uploads: dict[str, tuple[str, bool]] = {}
    test_inputs: dict[str, str] = {}
    for root in plan["roots"]:
        node = nodes[str(root["node_id"])]
        target = str(root["target"])
        version = str(root["version"])
        artifact_name = config.workflow.artifact_name.format(
            package=node["package"], target=target, version=version
        )
        if node["role"] == "test":
            artifact_id = str(node["artifact_dependencies"][0])
            artifact = nodes[artifact_id]
            uploads[artifact_id] = (_artifact_handoff(artifact), True)
            if config.publication is not None:
                uploads[str(node["id"])] = (
                    "ggbuild-publication-"
                    + str(node["cache_key"]).removeprefix("ggbuild-v3-"),
                    False,
                )
            test_inputs[str(node["id"])] = _artifact_handoff(artifact)
        else:
            uploads[str(node["id"])] = (artifact_name, False)

    matrices: list[
        tuple[str, tuple[str, ...], list[dict[str, str | bool]]]
    ] = []
    node_jobs: dict[str, str] = {}
    for index, layer in enumerate(plan["layers"]):
        target_nodes: dict[str, list[str]] = {}
        for raw_node_id in layer:
            node_id = str(raw_node_id)
            target = str(nodes[node_id]["target"])
            target_nodes.setdefault(target, []).append(node_id)

        for target, node_ids in target_nodes.items():
            job_id = f"nodes_{_job_component(target)}_{index}"
            dependency_jobs = tuple(
                sorted({
                    node_jobs[str(dependency)]
                    for node_id in node_ids
                    for dependency in nodes[node_id]["direct_dependencies"]
                })
            )
            entries = [
                _node_matrix_entry(config, nodes, node_id, uploads, test_inputs)
                for node_id in node_ids
            ]
            matrices.append((job_id, dependency_jobs, entries))
            for node_id in node_ids:
                node_jobs[node_id] = job_id
    return matrices


def _matrix_job(
    config: ProjectConfig,
    revision: str,
    *,
    job_id: str,
    dependencies: tuple[str, ...],
    entries: list[dict[str, str | bool]],
) -> str:
    needs = ", ".join(dependencies or ("planning",))
    matrix = _matrix_yaml(entries)
    return f"""  {job_id}:
    name: >-
      ${{{{ matrix.package }}}} ${{{{ matrix.version }}}}
      (${{{{ matrix.target }}}})
    if: >-
      github.event_name != 'workflow_dispatch' ||
      inputs.operation != 'ingest-existing'
    needs: [{needs}]
    runs-on: ${{{{ matrix.runner }}}}
    permissions:
      contents: read
      packages: read
    strategy:
      fail-fast: false
      max-parallel: {config.workflow.max_concurrency}
      matrix:
        include:
{matrix}
    steps:
      - uses: {_CHECKOUT}
        with:
          persist-credentials: false
      - name: Download canonical plan
        uses: {_DOWNLOAD}
        with:
          name: ggbuild-plan-v3
          path: .cache
      - name: Download prerequisite bundles
        if: matrix.has_bundles
        uses: {_DOWNLOAD}
        with:
          pattern: ggbuild-bundle-${{{{ matrix.target }}}}-*
          path: .cache/bundles
          merge-multiple: true
      - name: Restore exact bundle
        if: matrix.role == 'bundle'
        uses: {_CACHE}
        with:
          path: .cache/bundles/${{{{ matrix.cache_key }}}}.tar.zst
          key: ${{{{ matrix.cache_key }}}}
      - name: Download root artifact
        if: matrix.role == 'test'
        uses: {_DOWNLOAD}
        with:
          name: ${{{{ matrix.input_artifact }}}}
          path: dist/${{{{ matrix.target }}}}/${{{{ matrix.version }}}}
      - name: Log in to GitHub Container Registry
        if: matrix.execution == 'docker' || matrix.bare_linux_test
        uses: {_LOGIN}
        with:
          registry: ghcr.io
          username: ${{{{ github.actor }}}}
          password: ${{{{ secrets.GITHUB_TOKEN }}}}
      - name: Pull Linux runtime-test image
        if: matrix.bare_linux_test
        run: docker pull "${{{{ matrix.test_image }}}}"
      - name: Get sccache credentials
        if: env.GGBUILD_ENABLE_SCCACHE == 'true'
        uses: {_GITHUB_SCRIPT}
        with:
          script: |
            core.exportVariable(
              'ACTIONS_RESULTS_URL', process.env.ACTIONS_RESULTS_URL || '');
            core.exportVariable(
              'ACTIONS_RUNTIME_TOKEN',
              process.env.ACTIONS_RUNTIME_TOKEN || '');
      - uses: {config.workflow.setup_action}
        if: matrix.execution == 'host' || matrix.bare_linux_test
      - name: Execute node on host
        if: matrix.execution == 'host' && !matrix.bare_linux_test
        env:
          ACTIONS_CACHE_SERVICE_V2: on
          SCCACHE_GHA_ENABLED: on
        run: >-
          uv run ggbuild ci execute-node
          --plan .cache/ggbuild-plan.json
          --node "${{{{ matrix.node }}}}"
          --bundle-dir .cache
          --output-dir dist
          --work-dir .cache/work
          --max-parallel 1
          --exact
      - name: Execute Linux test node
        if: matrix.bare_linux_test
        run: >-
          uv run ggbuild ci execute-node
          --plan .cache/ggbuild-plan.json
          --node "${{{{ matrix.node }}}}"
          --bundle-dir .cache
          --output-dir dist
          --work-dir .cache/work
          --max-parallel 1
          --exact
          --bare-linux-test
      - name: Execute node in Linux image
        if: matrix.execution == 'docker' && !matrix.bare_linux_test
        env:
          ACTIONS_CACHE_SERVICE_V2: on
          GGBUILD_REF: {revision}
          SCCACHE_GHA_ENABLED: on
        run: >-
          docker run --rm
          --volume "$GITHUB_WORKSPACE:/github/workspace"
          --workdir /github/workspace
          --env GGBUILD_REF
          --env GGBUILD_ENABLE_SCCACHE
          --env ACTIONS_CACHE_SERVICE_V2
          --env ACTIONS_RESULTS_URL
          --env ACTIONS_RUNTIME_TOKEN
          --env SCCACHE_GHA_ENABLED
          "${{{{ matrix.build_image }}}}"
          ci execute-node
          --plan .cache/ggbuild-plan.json
          --node "${{{{ matrix.node }}}}"
          --bundle-dir .cache
          --output-dir dist
          --work-dir .cache/work
          --max-parallel 1
          --exact
          --prepared-target "${{{{ matrix.target }}}}"
      - name: Upload bundle
        if: matrix.role == 'bundle'
        uses: {_UPLOAD}
        with:
          name: ${{{{ matrix.bundle_artifact }}}}
          path: .cache/bundles/${{{{ matrix.cache_key }}}}.tar.zst
          if-no-files-found: error
          retention-days: 1
      - name: Upload root artifact
        if: matrix.role == 'artifact'
        uses: {_UPLOAD}
        with:
          name: ${{{{ matrix.upload_name }}}}
          path: dist/${{{{ matrix.target }}}}/${{{{ matrix.version }}}}/
          if-no-files-found: error
          retention-days: ${{{{ matrix.temporary_upload && 1 || 90 }}}}
      - name: Publish tested root artifact
        if: matrix.role == 'test' && matrix.publish_root
        uses: {_UPLOAD}
        with:
          name: ${{{{ matrix.upload_name }}}}
          path: dist/${{{{ matrix.target }}}}/${{{{ matrix.version }}}}/
          if-no-files-found: error
          retention-days: 1

"""


def _publication_ingestion_step(
    config: ProjectConfig,
    *,
    publication_tag: str,
    condition: str | None = None,
) -> str:
    publication = config.publication
    if publication is None or publication.index_url is None:
        return ""
    bypass_env = ""
    bypass_header = ""
    if publication.protection_bypass_secret is not None:
        secret = publication.protection_bypass_secret
        bypass_env = (
            "\n          PUBLICATION_PROTECTION_BYPASS: "
            f"${{{{ secrets.{secret} }}}}"
        )
        bypass_header = (
            "\n                'x-vercel-protection-bypass': "
            "process.env.PUBLICATION_PROTECTION_BYPASS,"
        )
    condition_line = f"\n        if: {condition}" if condition else ""
    return f"""
      - name: Ingest published snapshot index{condition_line}
        uses: {_GITHUB_SCRIPT}
        env:
          PUBLICATION_INDEX_URL: {publication.index_url}
          PUBLICATION_REPOSITORY: {publication.repository}
          PUBLICATION_TAG: {publication_tag}{bypass_env}
        with:
          script: |
            const token = await core.getIDToken(
              process.env.PUBLICATION_INDEX_URL);
            const response = await fetch(process.env.PUBLICATION_INDEX_URL, {{
              method: 'POST',
              headers: {{
                authorization: `Bearer ${{token}}`,
                'content-type': 'application/json',{bypass_header}
              }},
              body: JSON.stringify({{
                repository: process.env.PUBLICATION_REPOSITORY,
                tag: process.env.PUBLICATION_TAG,
              }}),
            }});
            if (!response.ok) {{
              const detail = await response.text();
              throw new Error(
                `index ingestion failed: ${{response.status}} ${{detail}}`);
            }}
"""


# @lat: [[orchestration#Project Orchestration#Workflow as Plan Projection]]
def render_workflow(
    config: ProjectConfig | None = None,
    *,
    ggbuild_revision: str | None = None,
) -> str:
    config = config or load_project()
    workflow = config.workflow
    revision = ggbuild_revision or ggbuild_source_revision()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("ggbuild revision must be a full lowercase Git SHA")
    plan = create_plan(config)
    plan_digest = digest_json(plan)
    sccache = config.sccache
    enabled = (
        "${{ "
        f"(github.event_name == 'workflow_dispatch' && inputs.enable_sccache) "
        f"|| (github.event_name == 'pull_request' && "
        f"{str(sccache.pull_request).lower()}) "
        f"|| (github.event_name == 'push' && "
        f"{str(sccache.production).lower()}) }}}}"
    )
    concurrency_group = (
        "${{ github.workflow }}-"
        "${{ github.event.pull_request.number || github.ref }}"
    )
    matrices = _node_matrices(config, plan)
    matrix_jobs = "".join(
        _matrix_job(
            config,
            revision,
            job_id=job_id,
            dependencies=dependencies,
            entries=entries,
        )
        for job_id, dependencies, entries in matrices
    )
    publication_job = ""
    if config.publication is not None:
        needs = ", ".join(["planning", *(item[0] for item in matrices)])
        condition = (
            "success() && github.repository == "
            f"'{config.publication.repository}' && "
            "(github.event_name != 'workflow_dispatch' || "
            "inputs.operation == 'build-and-publish') && "
            "((github.event_name == 'push' && github.ref == "
            f"'refs/heads/{workflow.branch}') || "
            "(github.event_name == 'workflow_dispatch' && github.ref == "
            f"'refs/heads/{workflow.branch}'))"
        )
        ingestion = _publication_ingestion_step(
            config,
            publication_tag="${{ steps.publish.outputs.tag }}",
            condition="steps.publish.outputs.published == 'true'",
        )
        publication_job = f"""
  publish:
    name: Publish immutable snapshot
    if: {condition}
    needs: [{needs}]
    runs-on: ubuntu-latest
    environment: release
    permissions:
      contents: write
      id-token: write
    steps:
      - uses: {_CHECKOUT}
        with:
          persist-credentials: false
      - uses: {workflow.setup_action}
      - name: Download canonical plan
        uses: {_DOWNLOAD}
        with:
          name: ggbuild-plan-v3
          path: .cache
      - name: Download every planned root
        uses: {_DOWNLOAD}
        with:
          pattern: ggbuild-publication-*
          path: .cache/publication
      - name: Publish GitHub snapshot
        id: publish
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
        run: >-
          uv run ggbuild ci publish-github
          --plan .cache/ggbuild-plan.json
          --artifacts .cache/publication
          --github-output "$GITHUB_OUTPUT"
{ingestion}"""
        if config.publication.index_url is not None:
            publication_job += f"""

  ingest_existing:
    name: Ingest existing immutable release
    if: >-
      github.repository == '{config.publication.repository}' &&
      github.event_name == 'workflow_dispatch' &&
      inputs.operation == 'ingest-existing' &&
      github.ref == 'refs/heads/{workflow.branch}'
    runs-on: ubuntu-latest
    environment: release
    permissions:
      contents: read
      id-token: write
    steps:
      - name: Validate release tag
        env:
          PUBLICATION_TAG: ${{{{ inputs.tag }}}}
        run: |
          if [[ ! "$PUBLICATION_TAG" =~ ^[0-9]{{12}}$ ]]; then
            echo "ingest-existing requires a twelve-digit tag" >&2
            exit 1
          fi
{_publication_ingestion_step(config, publication_tag="${{ inputs.tag }}")}"""
    return (
        f"""# Generated by `ggbuild ci render-workflow`; do not edit.
name: {workflow.name}

on:
  workflow_dispatch:
    inputs:
      operation:
        description: Build a new snapshot or ingest an existing release
        type: choice
        options:
          - build-and-publish
          - ingest-existing
        default: build-and-publish
      tag:
        description: Twelve-digit release tag for ingest-existing
        type: string
        required: false
      enable_sccache:
        description: Enable sccache for intermediate object files
        type: boolean
        default: {str(sccache.production).lower()}
  push:
    branches: [{workflow.branch}]
  pull_request:

permissions:
  contents: read

concurrency:
  group: {concurrency_group}
  cancel-in-progress: ${{{{ github.event_name == 'pull_request' }}}}

env:
  GGBUILD_ENABLE_SCCACHE: {enabled}

jobs:
  planning:
    if: >-
      github.event_name != 'workflow_dispatch' ||
      inputs.operation != 'ingest-existing'
    runs-on: ubuntu-latest
    steps:
      - uses: {_CHECKOUT}
        with:
          persist-credentials: false
      - uses: {workflow.setup_action}
      - name: Create canonical build plan
        run: >-
          uv run ggbuild ci plan
          --expected-digest {plan_digest}
          --output .cache/ggbuild-plan.json
      - name: Upload canonical plan
        uses: {_UPLOAD}
        with:
          name: ggbuild-plan-v3
          path: .cache/ggbuild-plan.json
          if-no-files-found: error
          retention-days: 1

{matrix_jobs}{publication_job}""".rstrip()
        + "\n"
    )


def generated_files(
    config: ProjectConfig | None = None,
    *,
    ggbuild_revision: str | None = None,
) -> dict[pathlib.Path, str]:
    config = config or load_project()
    return {
        config.root / config.workflow.path: render_workflow(
            config, ggbuild_revision=ggbuild_revision
        )
    }


def _write_atomic(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_generated(
    config: ProjectConfig | None = None,
    *,
    ggbuild_revision: str | None = None,
) -> tuple[pathlib.Path, ...]:
    files = generated_files(config, ggbuild_revision=ggbuild_revision)
    for path, content in files.items():
        _write_atomic(path, content)
    return tuple(files)


def check_generated(
    config: ProjectConfig | None = None,
    *,
    ggbuild_revision: str | None = None,
) -> None:
    config = config or load_project()
    expected = generated_files(config, ggbuild_revision=ggbuild_revision)
    stale = [
        str(path.relative_to(config.root))
        for path, content in expected.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    if stale:
        raise ValueError(
            "generated files are stale: "
            + ", ".join(stale)
            + "; run ggbuild ci render-workflow"
        )
