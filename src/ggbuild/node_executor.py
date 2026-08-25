# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Default v3 node executor."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pathlib
import shutil
import tarfile
import tempfile
import zipfile

import zstandard

from ggbuild.execution import (
    build_command,
    node_environment,
    run_child_build,
    run_docker_container,
)
from ggbuild.project import ProjectConfig, load_project
from ggbuild.publication import create_root_record
from ggbuild.testing import find_artifact

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ggbuild.ci_protocol import ExecutionPaths


class Executor(Protocol):
    def __call__(
        self, node: Mapping[str, object], paths: ExecutionPaths
    ) -> Sequence[pathlib.Path]: ...


def _extract_zstd_tar(source: pathlib.Path, destination: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        uncompressed = pathlib.Path(temporary) / "archive.tar"
        with (
            source.open("rb") as compressed,
            uncompressed.open("wb") as target,
        ):
            zstandard.ZstdDecompressor().copy_stream(compressed, target)
        with tarfile.open(uncompressed, mode="r:") as archive:
            archive.extractall(destination, filter="data")


def _extract_artifact(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if source.name.endswith((".tar.zst", ".tar.zstd")):
        _extract_zstd_tar(source, destination)
    elif source.name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        with tarfile.open(source, mode="r:*") as archive:
            archive.extractall(destination, filter="data")
    elif source.suffix == ".zip":
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                path = pathlib.PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe zip path: {member.filename}")
            archive.extractall(destination)  # ruff: ignore[tarfile-unsafe-members] - paths validated
    else:
        shutil.copy2(source, destination / source.name)


class DefaultNodeExecutor:
    def __init__(
        self,
        config: ProjectConfig | None = None,
        *,
        prepared_target: str | None = None,
        bare_linux_test: bool = False,
        enable_sccache: bool = False,
    ) -> None:
        self.config = config or load_project()
        self.prepared_target = prepared_target
        self.bare_linux_test = bare_linux_test
        self.enable_sccache = enable_sccache

    def __call__(
        self, node: Mapping[str, object], paths: ExecutionPaths
    ) -> Sequence[pathlib.Path]:
        return self.build_node(node, paths)

    def build_node(  # ruff: ignore[too-many-branches]
        self, node: Mapping[str, object], paths: ExecutionPaths
    ) -> Sequence[pathlib.Path]:
        raw_output = paths.work_dir / "raw-output"
        raw_output.mkdir(parents=True, exist_ok=True)
        environment = node_environment(node, paths, self.config)
        if self.enable_sccache:
            environment["GGBUILD_ENABLE_SCCACHE"] = "1"
        if self.prepared_target is not None:
            environment["LOGRAIL_OUTPUT"] = "json"
        pathlib.Path(environment["GGBUILD_CONTAINER_CACHE"]).mkdir(
            parents=True, exist_ok=True
        )
        target = self.config.target(str(node["target"]))
        subject = f"{node['package']} {node['version']} for {node['target']}"
        if (
            target.execution == "docker"
            and self.prepared_target is None
            and not (node["role"] == "test" and self.bare_linux_test)
        ):
            run_docker_container(
                node,
                paths,
                self.config,
                raw_output=raw_output,
                environment=environment,
                subject=subject,
            )
        else:
            command = build_command(
                node,
                paths,
                self.config,
                environment=environment,
                prepared_target=self.prepared_target,
                bare_linux_test=self.bare_linux_test,
            )
            run_child_build(
                command,
                environment=environment,
                subject=subject,
                structured=self.prepared_target is not None,
            )
        outputs = tuple(
            sorted(path for path in raw_output.iterdir() if path.is_file())
        )
        if node["role"] == "test":
            if len(outputs) != 1 or outputs[0].name != "test-result.json":
                raise RuntimeError(
                    f"node {node['id']} produced no deterministic test result"
                )
            destination = (
                paths.output_dir
                / "test-results"
                / str(node["target"])
                / f"{node['version']}.json"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(outputs[0], destination)
            if self.config.publication is not None:
                artifact_directory = (
                    paths.output_dir
                    / str(node["target"])
                    / str(node["version"])
                )
                archive = find_artifact(artifact_directory)
                persistent_result = archive.with_name(
                    archive.name + ".test-result.json"
                )
                shutil.copy2(destination, persistent_result)
                record = create_root_record(node, artifact_directory)
                return (destination, persistent_result, record)
            return (destination,)
        if node["role"] == "artifact":
            if not outputs:
                raise RuntimeError(f"node {node['id']} produced no output")
            destination = (
                paths.output_dir / str(node["target"]) / str(node["version"])
            )
            destination.mkdir(parents=True, exist_ok=True)
            published: list[pathlib.Path] = []
            for source in outputs:
                target_path = destination / source.name
                shutil.copy2(source, target_path)
                published.append(target_path)
            return published
        staging_root = paths.work_dir / "sysroot"
        if not staging_root.is_dir() or not any(staging_root.iterdir()):
            for source in outputs:
                _extract_artifact(source, staging_root)
        if not staging_root.is_dir() or not any(staging_root.iterdir()):
            raise RuntimeError(
                f"node {node['id']} produced no staged installation"
            )
        return (staging_root,)


def default_executor(
    node: Mapping[str, object], paths: ExecutionPaths
) -> Sequence[pathlib.Path]:
    return DefaultNodeExecutor().build_node(node, paths)
