from __future__ import annotations

from typing import Any, cast

import dataclasses
import io
import json
import os
import pathlib
import tarfile
from unittest import mock

import pytest
import zstandard

import ggbuild.app  # ruff: ignore[unused-import]  # Initialize import graph.
from ggbuild import packages
from ggbuild.execution import parse_target
from ggbuild.packages import BundledPackage
from ggbuild.planner import PlanOptions, create_plan
from ggbuild.project import (
    BuildOptions,
    ProjectConfig,
    TargetConfig,
    WorkflowConfig,
)
from ggbuild.testing import (
    extract_artifact,
    metadata_path,
    run_test,
    side_artifacts,
    test_environment as make_test_environment,
    test_is_privileged_root as is_privileged_root,
)

TARGET = "x86_64-unknown-linux-gnu"


def config(tmp_path: pathlib.Path, *, tested: bool = True) -> ProjectConfig:
    recipe = (
        "tests.artifact_recipe:Tested"
        if tested
        else "tests.artifact_recipe:Untested"
    )
    return ProjectConfig(
        root=tmp_path,
        project_name="artifact-test",
        root_recipe=recipe,
        release_selection="all",
        portable=True,
        bundle_prefix="fixture",
        targets=(TargetConfig(TARGET, "host", "ubuntu-latest"),),
        workflow=WorkflowConfig(),
        build_options=BuildOptions(),
    )


def artifact(tmp_path: pathlib.Path, payload: str = "ok") -> pathlib.Path:
    archive = tmp_path / "tested__1.0__linux.tar.zst"
    raw = io.BytesIO()
    data = payload.encode()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        directory = tarfile.TarInfo("tested__1.0__linux")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        member = tarfile.TarInfo("tested__1.0__linux/payload")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    archive.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    metadata_path(archive).write_text(
        json.dumps({
            "contents": {archive.name: {"encoding": "zstd"}},
            "name": "tested",
            "source_version": "1.0",
            "target": TARGET,
        }),
        encoding="utf-8",
    )
    return archive


def test_runs_exact_recipe_hook_and_writes_deterministic_result(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = artifact(tmp_path)
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target(TARGET),
    )
    work = tmp_path / "work"

    first, result = run_test(archive, config=config(tmp_path), work_dir=work)
    second, _ = run_test(archive, config=config(tmp_path), work_dir=work)

    assert first == second
    assert first["version"] == "1.0"
    assert first["target"] == TARGET
    assert first["recipe"] == "tests.artifact_recipe:Tested"
    assert (work / "packages/tested-1.0/hook-ran").read_text(
        encoding="utf-8"
    ) == "1.0"
    assert json.loads(result.read_text(encoding="utf-8")) == first


def test_bare_linux_test_uses_isolated_abi_floor_container(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = artifact(tmp_path)
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target(TARGET),
    )
    completed = mock.patch("ggbuild.testing.subprocess.run")
    with completed as run:
        run_test(
            archive,
            config=config(tmp_path),
            work_dir=tmp_path / "work",
            bare_linux=True,
        )

    command = run.call_args.args[0]
    assert command[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in command
    assert "--network=none" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert any(value.startswith("--user=") for value in command)
    assert any(value.startswith("--tmpfs=/tmp:") for value in command)
    assert any(value.startswith("--volume=") for value in command)
    assert any(
        value.startswith("ghcr.io/vercel-labs/ggbuild/test-linux-gnu:")
        for value in command
    )


def test_bare_musl_test_uses_minimal_musl_container(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = artifact(tmp_path)
    metadata = json.loads(
        metadata_path(archive_path).read_text(encoding="utf-8")
    )
    musl_target = "x86_64-unknown-linux-musl"
    metadata["target"] = musl_target
    metadata_path(archive_path).write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    project = dataclasses.replace(
        config(tmp_path),
        targets=(TargetConfig(musl_target, "host", "ubuntu-latest"),),
    )
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target(musl_target),
    )
    with mock.patch("ggbuild.testing.subprocess.run") as run:
        run_test(
            archive_path,
            config=project,
            work_dir=tmp_path / "work",
            bare_linux=True,
        )

    command = run.call_args.args[0]
    assert any(
        value.startswith("ghcr.io/vercel-labs/ggbuild/test-linux-musl:")
        for value in command
    )


def test_rejects_missing_hook_host_mismatch_and_hook_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = artifact(tmp_path)
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target(TARGET),
    )
    with pytest.raises(ValueError, match="no test hook"):
        run_test(
            archive,
            config=config(tmp_path, tested=False),
            work_dir=tmp_path / "a",
        )
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target("aarch64-unknown-linux-gnu"),
    )
    with pytest.raises(ValueError, match="exact native host match"):
        run_test(archive, config=config(tmp_path), work_dir=tmp_path / "b")
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target(TARGET),
    )
    with pytest.raises(RuntimeError, match="script failed"):
        run_test(
            artifact(tmp_path, "fail"),
            config=config(tmp_path),
            work_dir=tmp_path / "c",
        )


def test_safe_extraction_rejects_parent_paths(tmp_path: pathlib.Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe archive path"):
        extract_artifact(archive, tmp_path / "extract")


def test_side_artifact_roles_are_unique(tmp_path: pathlib.Path) -> None:
    archive = tmp_path / "primary.tar.zst"
    first = tmp_path / "first.tar.zst"
    second = tmp_path / "second.tar.zst"
    first.touch()
    second.touch()
    with pytest.raises(ValueError, match="duplicate artifact side role"):
        side_artifacts(
            archive,
            {
                "contents": {
                    first.name: {"artifact_role": "test-data"},
                    second.name: {"artifact_role": "test-data"},
                }
            },
        )


def test_test_data_side_artifact_overlays_installation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = artifact(tmp_path)
    side = tmp_path / "tested__1.0__linux.test-data.tar.zst"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        data = b"staged"
        directory = tarfile.TarInfo(
            "tested__1.0__linux/.ggbuild-test-data/source"
        )
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        member = tarfile.TarInfo(
            "tested__1.0__linux/.ggbuild-test-data/source/input"
        )
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    side.write_bytes(zstandard.ZstdCompressor().compress(raw.getvalue()))
    metadata = json.loads(metadata_path(archive).read_text(encoding="utf-8"))
    metadata["contents"][side.name] = {
        "artifact_role": "test-data",
        "overlay": True,
        "root": ".ggbuild-test-data",
        "paths": {"tested-1.0": {"source": "source"}},
    }
    metadata_path(archive).write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(
        "ggbuild.testing.detect_host_target",
        lambda: parse_target(TARGET),
    )

    run_test(archive, config=config(tmp_path), work_dir=tmp_path / "work")

    installation = tmp_path / "work/installation/tested__1.0__linux"
    assert (installation / "payload").read_text(encoding="utf-8") == "ok"
    assert (installation / ".ggbuild-test-data/source/input").read_text(
        encoding="utf-8"
    ) == "staged"


@pytest.mark.parametrize(
    ("target", "library_variable"),
    [
        (TARGET, "LD_LIBRARY_PATH"),
        ("aarch64-apple-darwin", "DYLD_LIBRARY_PATH"),
    ],
)
def test_environment_is_isolated(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    library_variable: str,
) -> None:
    monkeypatch.setenv("PATH", "/system/bin")
    monkeypatch.setenv(library_variable, "/system/lib")
    monkeypatch.setenv("TMPDIR", "/system/tmp")
    recipe = cast("BundledPackage", mock.Mock(unique_name="fixture"))
    get_test_env = mock.patch.object(recipe, "get_test_env", return_value={})
    test = packages.Test(
        archive=tmp_path / "artifact.tar.zst",
        metadata={},
        target=target,
        source_version="1.0",
        _root_package="fixture",
        _installation_root=tmp_path / "installation",
        _work_root=tmp_path / "work",
    )

    with get_test_env:
        environment = make_test_environment(test, recipe)
    work = test.get_temp_dir(recipe)
    installation = test.get_build_install_dir(recipe)

    assert environment["HOME"] == str(work / "home")
    assert environment["TMPDIR"] == "/system/tmp"
    assert environment["PATH"].split(os.pathsep)[0] == str(installation / "bin")
    assert environment[library_variable].split(os.pathsep) == [
        str(installation / "lib"),
        "/system/lib",
    ]
    assert (work / "home").is_dir()


def test_test_resolves_install_roots_and_isolates_temp_dirs(
    tmp_path: pathlib.Path,
) -> None:
    test_install = tmp_path / "test-install"
    test_install.mkdir()
    package = cast("BundledPackage", mock.Mock(unique_name="fixture"))
    test = packages.Test(
        archive=tmp_path / "artifact.tar.zst",
        metadata={},
        target=TARGET,
        source_version="1.0",
        _root_package="fixture",
        _installation_root=tmp_path / "installation",
        _work_root=tmp_path / "work",
        _test_install_root=test_install,
    )

    assert test.get_build_install_dir(package) == tmp_path / "installation"
    assert test.get_test_install_dir(package) == test_install
    assert test.get_temp_dir(package) == tmp_path / "work/packages/fixture"
    assert test.get_temp_dir(package).is_dir()


def test_test_rejects_missing_sidecar_and_non_root_package_paths(
    tmp_path: pathlib.Path,
) -> None:
    root = cast("BundledPackage", mock.Mock(unique_name="root"))
    dependency = cast("BundledPackage", mock.Mock(unique_name="dependency"))
    test = packages.Test(
        archive=tmp_path / "artifact.tar.zst",
        metadata={},
        target=TARGET,
        source_version="1.0",
        _root_package="root",
        _installation_root=tmp_path / "installation",
        _work_root=tmp_path / "work",
    )

    with pytest.raises(ValueError, match="only the root package"):
        test.get_build_install_dir(dependency)
    with pytest.raises(ValueError, match="test sidecar is missing"):
        test.get_test_install_dir(root)
    with pytest.raises(ValueError, match="only the root package"):
        test.get_test_install_dir(dependency)


def test_privilege_detection_allows_rootless_root() -> None:
    with (
        mock.patch("ggbuild.testing.os.geteuid", return_value=0, create=True),
        mock.patch("pathlib.Path.read_text", return_value="0 1000 1\n"),
    ):
        assert not is_privileged_root()
    with (
        mock.patch("ggbuild.testing.os.geteuid", return_value=0, create=True),
        mock.patch("pathlib.Path.read_text", return_value="0 0 4294967295\n"),
    ):
        assert is_privileged_root()


def test_tested_recipes_get_test_roots_and_untested_recipes_do_not(
    tmp_path: pathlib.Path,
) -> None:
    tested = create_plan(config(tmp_path), PlanOptions(versions=("1.0",)))
    untested = create_plan(
        config(tmp_path, tested=False), PlanOptions(versions=("1.0",))
    )
    nodes: dict[str, dict[str, Any]] = {
        node["role"]: node for node in tested["nodes"]
    }
    assert tested["roots"][0]["node_id"] == nodes["test"]["id"]
    assert nodes["test"]["artifact_dependencies"] == [nodes["artifact"]["id"]]
    assert tested["layers"] == [
        [nodes["artifact"]["id"]],
        [nodes["test"]["id"]],
    ]
    assert untested["roots"][0]["node_id"] == untested["nodes"][0]["id"]
    assert all(node["role"] != "test" for node in untested["nodes"])
