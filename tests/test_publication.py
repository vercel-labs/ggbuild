# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import dataclasses
import json
import operator
import pathlib
import shutil

import pytest

from ggbuild import publication
from ggbuild.ci_protocol import PlanNode, canonical_json, sha256_file
from ggbuild.project import ProjectConfig, PublicationConfig
from ggbuild.publication import (
    CHECKSUM_NAME,
    SNAPSHOT_NAME,
    create_root_record,
    publish_github,
)
from ggbuild.testing import metadata_path
from tests.test_ci_v2 import TARGET, fixture_config


def _plan() -> dict[str, Any]:
    artifact = PlanNode(
        id="artifact",
        role="artifact",
        package="fixture",
        recipe="tests.v2_recipe:Root",
        version="1.0",
        target=TARGET,
        cache_key="ggbuild-v3-artifact",
        direct_dependencies=(),
        runtime_dependencies=(),
        build_dependencies=(),
        artifact_dependencies=(),
        bundle_install_subdir="fixture",
        installation_path=None,
        expected_outputs=(),
        inputs={},
    ).as_dict()
    test = PlanNode(
        id="test",
        role="test",
        package="fixture",
        recipe="tests.v2_recipe:Root",
        version="1.0",
        target=TARGET,
        cache_key="ggbuild-v3-test",
        direct_dependencies=("artifact",),
        runtime_dependencies=(),
        build_dependencies=(),
        artifact_dependencies=("artifact",),
        bundle_install_subdir="fixture",
        installation_path=None,
        expected_outputs=(),
        inputs={"artifact_key": "ggbuild-v3-artifact"},
    ).as_dict()
    return {
        "build_options": {},
        "format_version": 3,
        "layers": [["artifact"], ["test"]],
        "nodes": [artifact, test],
        "project": "fixture",
        "resolved_packages": {},
        "roots": [{"node_id": "test", "target": TARGET, "version": "1.0"}],
        "targets": [TARGET],
    }


def _two_root_plan() -> dict[str, Any]:
    plan = _plan()
    artifact = {
        **plan["nodes"][0],
        "cache_key": "ggbuild-v3-artifact-2",
        "id": "artifact-2",
        "version": "2.0",
    }
    test = {
        **plan["nodes"][1],
        "artifact_dependencies": ["artifact-2"],
        "cache_key": "ggbuild-v3-test-2",
        "direct_dependencies": ["artifact-2"],
        "id": "test-2",
        "inputs": {"artifact_key": "ggbuild-v3-artifact-2"},
        "version": "2.0",
    }
    plan["nodes"].extend((artifact, test))
    plan["layers"] = [["artifact", "artifact-2"], ["test", "test-2"]]
    plan["roots"].append({
        "node_id": "test-2",
        "target": TARGET,
        "version": "2.0",
    })
    return plan


def _root(directory: pathlib.Path) -> pathlib.Path:
    archive = directory / "fixture.tar.zst"
    archive.write_bytes(b"archive")
    side = directory / "fixture.dbgsym.tar.zst"
    side.write_bytes(b"dbgsym")
    metadata_path(archive).write_text(
        canonical_json({
            "contents": {
                archive.name: {
                    "encoding": "zstd",
                    "type": "application/x-tar",
                },
                side.name: {
                    "artifact_role": "dbgsym",
                    "encoding": "zstd",
                    "type": "application/x-tar",
                },
            },
            "name": "fixture",
            "source_version": "1.0",
            "target": TARGET,
        }),
        encoding="utf-8",
    )
    archive.with_name(archive.name + ".test-result.json").write_text(
        canonical_json({
            "artifact_sha256": sha256_file(archive),
            "recipe": "tests.v2_recipe:Root",
            "target": TARGET,
            "version": "1.0",
        }),
        encoding="utf-8",
    )
    return create_root_record(_plan()["nodes"][1], directory)


def _second_root(directory: pathlib.Path) -> pathlib.Path:
    archive = directory / "fixture-2.tar.zst"
    archive.write_bytes(b"archive-2")
    side = directory / "fixture-2.dbgsym.tar.zst"
    side.write_bytes(b"dbgsym-2")
    metadata_path(archive).write_text(
        canonical_json({
            "contents": {
                archive.name: {
                    "encoding": "zstd",
                    "type": "application/x-tar",
                },
                side.name: {
                    "artifact_role": "dbgsym",
                    "encoding": "zstd",
                    "type": "application/x-tar",
                },
            },
            "name": "fixture",
            "source_version": "2.0",
            "target": TARGET,
        }),
        encoding="utf-8",
    )
    archive.with_name(archive.name + ".test-result.json").write_text(
        canonical_json({
            "artifact_sha256": sha256_file(archive),
            "recipe": "tests.v2_recipe:Root",
            "target": TARGET,
            "version": "2.0",
        }),
        encoding="utf-8",
    )
    return create_root_record(_two_root_plan()["nodes"][3], directory)


class Client:
    api = "https://api.github.test/repos/example/project"

    def __init__(self) -> None:
        self.release: dict[str, Any] | None = None
        self.run_lookups = 0

    def run_started_at(self, run_id: str) -> str:
        assert run_id == "42"
        self.run_lookups += 1
        return "2026-01-02T03:04:05Z"

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        assert tag == "202601020304"
        return self.release

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        raise AssertionError((method, url, kwargs))


def _environment() -> dict[str, str]:
    return {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_ID": "42",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_WORKFLOW_REF": (
            "example/project/.github/workflows/build.yml@refs/heads/main"
        ),
    }


def _config(tmp_path: pathlib.Path) -> ProjectConfig:
    return dataclasses.replace(
        fixture_config(tmp_path),
        publication=PublicationConfig("example/project"),
    )


def test_root_record_binds_test_and_classifies_files(
    tmp_path: pathlib.Path,
) -> None:
    record = json.loads(_root(tmp_path).read_text(encoding="utf-8"))
    assert record["coordinate"] == {
        "package": "fixture",
        "source_version": "1.0",
        "target": TARGET,
    }
    roles = {item["logical_name"]: item["role"] for item in record["files"]}
    assert roles["fixture.tar.zst"] == "primary-archive"
    assert roles["fixture.dbgsym.tar.zst"] == "dbgsym"
    assert roles["fixture.metadata.json"] == "metadata"
    assert roles["fixture.tar.zst.test-result.json"] == "test-result"
    assert record["test_result"]["artifact_sha256"] == sha256_file(
        tmp_path / "fixture.tar.zst"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda path: path.with_name("unexpected").write_bytes(b"x"),
            "inventory",
        ),
        (lambda path: path.unlink(), "expected one testable artifact"),
        (lambda path: path.write_bytes(b"changed"), "bound"),
    ],
)
def test_root_record_rejects_unexpected_missing_or_untested_bytes(
    tmp_path: pathlib.Path,
    mutation: Any,
    message: str,
) -> None:
    record = _root(tmp_path)
    archive = tmp_path / "fixture.tar.zst"
    record.unlink()
    mutation(archive)
    with pytest.raises(ValueError, match=message):
        create_root_record(_plan()["nodes"][1], tmp_path)


def test_publish_dry_run_validates_complete_success(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _root(root)
    snapshot, tag = publish_github(
        _plan(),
        tmp_path,
        _config(tmp_path),
        dry_run=True,
        environment=_environment(),
        client=Client(),
    )
    assert tag == "202601020304"
    assert snapshot is not None
    assert len(snapshot["expected_coordinates"]) == 1
    assert len(snapshot["successful"]) == 1
    public = [
        item
        for item in snapshot["successful"][0]["files"]
        if "release_name" in item
    ]
    assert {item["release_name"] for item in public} == {
        f"fixture-1.0+{tag}-{TARGET}.tar.zst",
        f"fixture-1.0+{tag}-{TARGET}-dbgsym.tar.zst",
    }
    assert all(
        "release_name" not in item
        for item in snapshot["successful"][0]["files"]
        if item["role"] not in {"primary-archive", "dbgsym"}
    )


def test_publish_complete_success_contains_every_expected_coordinate(
    tmp_path: pathlib.Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _root(first)
    _second_root(second)
    snapshot, _ = publish_github(
        _two_root_plan(),
        tmp_path,
        _config(tmp_path),
        dry_run=True,
        environment=_environment(),
        client=Client(),
    )
    assert {
        tuple(coordinate.values())
        for coordinate in snapshot["expected_coordinates"]
    } == {
        tuple(record["coordinate"].values())
        for record in snapshot["successful"]
    }


@pytest.mark.parametrize("dry_run", [False, True])
def test_publish_missing_root_rejected_before_github_mutation(
    tmp_path: pathlib.Path,
    *,
    dry_run: bool,
) -> None:
    client = Client()
    with pytest.raises(ValueError, match="exactly match planned roots"):
        publish_github(
            _plan(),
            tmp_path,
            _config(tmp_path),
            dry_run=dry_run,
            environment=_environment(),
            client=client,
        )
    assert client.run_lookups == 0


def test_publish_rejects_tampered_and_duplicate_records(
    tmp_path: pathlib.Path,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    record = _root(first)
    value = json.loads(record.read_text(encoding="utf-8"))
    value["files"][0]["role"] = "test-data"
    record.write_text(canonical_json(value), encoding="utf-8")
    with pytest.raises(ValueError, match="not canonical"):
        publish_github(
            _plan(),
            tmp_path,
            _config(tmp_path),
            dry_run=True,
            environment=_environment(),
            client=Client(),
        )
    _root(first)
    second = tmp_path / "second"
    shutil.copytree(first, second)
    with pytest.raises(ValueError, match="duplicate successful root"):
        publish_github(
            _plan(),
            tmp_path,
            _config(tmp_path),
            dry_run=True,
            environment=_environment(),
            client=Client(),
        )


def test_publish_rejects_extra_root_record(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    record = _root(root)
    value = json.loads(record.read_text(encoding="utf-8"))
    value["test_node"] = "unplanned-test"
    record.write_text(canonical_json(value), encoding="utf-8")
    with pytest.raises(ValueError, match="not a canonical root"):
        publish_github(
            _plan(),
            tmp_path,
            _config(tmp_path),
            dry_run=True,
            environment=_environment(),
            client=Client(),
        )


class ReleaseClient:
    api = "https://api.github.test/repos/example/project"

    def __init__(self, release: dict[str, Any] | None = None) -> None:
        self.release = release
        self.assets: dict[str, bytes] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        return self.release

    def run_started_at(self, run_id: str) -> str:
        raise AssertionError(run_id)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/releases"):
            self.release = {
                "assets": [],
                "draft": True,
                "id": 1,
                "upload_url": "https://uploads.github.test/{?name}",
            }
            return Response(self.release)
        if method == "POST":
            name = kwargs["params"]["name"]
            self.assets[name] = kwargs["data"]
            return Response({"name": name, "url": f"asset://{name}"})
        if method == "GET":
            return Response({}, self.assets[url.removeprefix("asset://")])
        return Response({})


class Response:
    def __init__(self, value: object, content: bytes = b"") -> None:
        self.value = value
        self.content = content

    def json(self) -> object:
        return self.value


def test_distributions_then_checksum_then_snapshot_are_uploaded(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _root(root)
    snapshot, _ = publish_github(
        _plan(),
        tmp_path,
        _config(tmp_path),
        dry_run=True,
        environment=_environment(),
        client=Client(),
    )
    assert snapshot is not None
    client = ReleaseClient()
    checksum = "".join(
        f"{item['sha256']}  {item['release_name']}\n"
        for item in sorted(
            (
                item
                for record in snapshot["successful"]
                for item in record["files"]
                if "release_name" in item
            ),
            key=operator.itemgetter("release_name"),
        )
    ).encode()
    publication._publish_snapshot(  # ruff: ignore[private-member-access]
        client, snapshot, checksum, tmp_path
    )
    uploads = [
        call[2]["params"]["name"]
        for call in client.calls
        if call[0] == "POST" and "params" in call[2]
    ]
    assert uploads[-2:] == [CHECKSUM_NAME, SNAPSHOT_NAME]
    assert client.calls[-1][0] == "PATCH"
    create = next(
        call for call in client.calls if call[1].endswith("/releases")
    )
    assert create[2]["json"]["name"] == "202601020304"
    assert create[2]["json"]["body"].endswith("ggbuild-snapshot-v1.json\n")
    assert client.calls[-1][2]["json"] == {
        "draft": False,
        "prerelease": False,
        "make_latest": "true",
    }


@pytest.mark.parametrize("draft", [False, True])
def test_existing_release_is_always_rejected_before_mutation(
    tmp_path: pathlib.Path,
    *,
    draft: bool,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _root(root)
    snapshot, _ = publish_github(
        _plan(),
        tmp_path,
        _config(tmp_path),
        dry_run=True,
        environment=_environment(),
        client=Client(),
    )
    assert snapshot is not None
    release = {"assets": [], "draft": draft, "tag_name": "202601020304"}
    client = ReleaseClient(release)
    with pytest.raises(ValueError, match="already exists"):
        publication._publish_snapshot(  # ruff: ignore[private-member-access]
            client, snapshot, b"", tmp_path
        )
    assert client.calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_release_is_rejected_by_entrypoint(
    tmp_path: pathlib.Path, *, dry_run: bool
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _root(root)
    client = Client()
    client.release = {"draft": True, "tag_name": "202601020304"}
    with pytest.raises(ValueError, match="already exists"):
        publish_github(
            _plan(),
            tmp_path,
            _config(tmp_path),
            dry_run=dry_run,
            environment=_environment(),
            client=client,
        )
