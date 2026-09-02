# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0

"""Strict, deterministic publication of complete build snapshots."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import datetime
import hashlib
import json
import mimetypes
import os
import pathlib
import urllib.parse

import requests

from ggbuild.ci_protocol import canonical_json, node_map, sha256_file
from ggbuild.testing import find_artifact, load_artifact_metadata, metadata_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ggbuild.project import ProjectConfig

PUBLICATION_RECORD = "ggbuild-publication.json"
CHECKSUM_NAME = "SHA256SUMS"
SNAPSHOT_NAME = "ggbuild-snapshot-v1.json"
FileRole = Literal[
    "primary-archive", "dbgsym", "test-data", "metadata", "test-result"
]
PUBLIC_ROLES = frozenset({"primary-archive", "dbgsym"})


def _file_entry(
    path: pathlib.Path,
    *,
    role: FileRole,
    media_type: str | None = None,
    encoding: str = "identity",
) -> dict[str, object]:
    return {
        "encoding": encoding,
        "logical_name": path.name,
        "media_type": media_type
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream",
        "role": role,
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


# @lat: [[artifacts#Artifact Trust Pipeline#Test-to-Publication Binding]]
def create_root_record(  # ruff: ignore[too-many-branches,too-many-locals,too-many-statements]
    node: Mapping[str, object], artifact_dir: pathlib.Path
) -> pathlib.Path:
    """Bind one successful test to its exact coordinate and artifact bytes."""
    archive = find_artifact(artifact_dir)
    metadata = load_artifact_metadata(archive)
    if (
        metadata["name"] != node["package"]
        or metadata["source_version"] != node["version"]
        or metadata["target"] != node["target"]
    ):
        raise ValueError("tested artifact coordinate does not match plan node")
    result_path = archive.with_name(archive.name + ".test-result.json")
    if not result_path.is_file():
        raise ValueError(
            "successful test did not persist beside primary archive"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    expected_result = {
        "artifact_sha256": sha256_file(archive),
        "recipe": node["recipe"],
        "target": node["target"],
        "version": node["version"],
    }
    if not isinstance(result, dict) or result != expected_result:
        raise ValueError(
            "test result is not bound to the plan and primary archive"
        )
    contents = metadata.get("contents")
    if not isinstance(contents, dict) or not contents:
        raise ValueError("artifact metadata has no declared contents")
    declared = set(cast("dict[str, object]", contents))
    expected = declared | {metadata_path(archive).name, result_path.name}
    actual = {
        path.name
        for path in artifact_dir.iterdir()
        if path.is_file() and path.name != PUBLICATION_RECORD
    }
    if actual != expected:
        raise ValueError(
            "publication inventory mismatch; "
            f"missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    entries: list[dict[str, object]] = []
    primary = 0
    dbgsym = 0
    for filename, raw_details in sorted(contents.items()):
        if (
            not isinstance(filename, str)
            or pathlib.PurePath(filename).name != filename
        ):
            raise ValueError("artifact metadata contains an unsafe filename")
        if filename.endswith(".debug.tar.zst"):
            raise ValueError("legacy .debug.tar.zst artifacts are unsupported")
        if not isinstance(raw_details, dict):
            raise TypeError("artifact content details must be an object")
        details = cast("dict[str, object]", raw_details)
        raw_role = details.get("artifact_role")
        if raw_role is None:
            role: FileRole = "primary-archive"
            primary += 1
            if not filename.endswith(".tar.zst"):
                raise ValueError("primary artifact filename is not canonical")
        elif raw_role == "dbgsym":
            role = "dbgsym"
            dbgsym += 1
            if not filename.endswith(".dbgsym.tar.zst"):
                raise ValueError("dbgsym artifact filename is not canonical")
        elif raw_role == "test-data":
            role = "test-data"
        else:
            raise ValueError(f"unsupported artifact role: {raw_role!r}")
        media_type = details.get("type")
        encoding = details.get("encoding", "identity")
        if not isinstance(media_type, str) or not isinstance(encoding, str):
            raise TypeError(
                "artifact content type and encoding must be strings"
            )
        if role in PUBLIC_ROLES and (
            media_type != "application/x-tar" or encoding != "zstd"
        ):
            raise ValueError("public artifact encoding is not canonical")
        entries.append(
            _file_entry(
                artifact_dir / filename,
                role=role,
                media_type=media_type,
                encoding=encoding,
            )
        )
    if primary != 1:
        raise ValueError("publication requires exactly one primary archive")
    if dbgsym != 1:
        raise ValueError("publication requires exactly one dbgsym archive")
    entries.extend((
        _file_entry(
            metadata_path(archive),
            role="metadata",
            media_type="application/json",
        ),
        _file_entry(
            result_path, role="test-result", media_type="application/json"
        ),
    ))
    logical_names = [str(item["logical_name"]) for item in entries]
    if len(logical_names) != len(set(logical_names)):
        raise ValueError("publication has colliding logical filenames")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict) or not isinstance(
        inputs.get("artifact_key"), str
    ):
        raise TypeError("test node has no artifact binding")
    record = {
        "artifact_cache_key": inputs["artifact_key"],
        "coordinate": {
            "package": node["package"],
            "source_version": node["version"],
            "target": node["target"],
        },
        "files": sorted(entries, key=lambda item: str(item["logical_name"])),
        "format": "ggbuild-publication-v1",
        "test_node": node["id"],
        "test_result": result,
    }
    destination = artifact_dir / PUBLICATION_RECORD
    destination.write_text(canonical_json(record), encoding="utf-8")
    return destination


def _validate_record(
    value: object,
    directory: pathlib.Path,
    root: Mapping[str, object],
    nodes: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("format") != "ggbuild-publication-v1"
    ):
        raise ValueError("invalid ggbuild publication record")
    record = cast("dict[str, Any]", value)
    node_id = root["node_id"]
    node = nodes[str(node_id)]
    if node["role"] != "test" or record.get("test_node") != node_id:
        raise ValueError(
            "publication record does not match canonical test root"
        )
    expected_coordinate = {
        "package": node["package"],
        "source_version": root["version"],
        "target": root["target"],
    }
    if record.get("coordinate") != expected_coordinate:
        raise ValueError("publication coordinate does not match canonical plan")
    artifact_id = node["artifact_dependencies"][0]
    if record.get("artifact_cache_key") != nodes[artifact_id]["cache_key"]:
        raise ValueError(
            "publication artifact binding does not match canonical plan"
        )
    canonical_path = create_root_record(node, directory)
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    if canonical != record:
        raise ValueError("publication record is not canonical")
    return cast("dict[str, Any]", canonical)


class GitHubClient:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.api = f"https://api.github.com/repos/{repository}"

    def request(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        response = self.session.request(method, url, timeout=60, **kwargs)
        response.raise_for_status()
        return response

    def run_started_at(self, run_id: str) -> str:
        value = self.request("GET", f"{self.api}/actions/runs/{run_id}").json()
        started = value.get("run_started_at")
        if not isinstance(started, str):
            raise TypeError("GitHub run has no authoritative start time")
        return started

    def release_by_tag(self, tag: str) -> dict[str, Any] | None:
        page = 1
        while True:
            value = self.request(
                "GET",
                f"{self.api}/releases",
                params={"page": page, "per_page": 100},
            ).json()
            if not isinstance(value, list):
                raise TypeError("GitHub releases response is malformed")
            for release in value:
                if isinstance(release, dict) and release.get("tag_name") == tag:
                    return cast("dict[str, Any]", release)
            if len(value) < 100:
                return None
            page += 1


class GitHubPublisher(Protocol):
    api: str

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...

    def run_started_at(self, run_id: str) -> str: ...

    def release_by_tag(self, tag: str) -> dict[str, Any] | None: ...


def _tag(started_at: str) -> str:
    started = datetime.datetime.fromisoformat(started_at)
    if started.tzinfo is None:
        raise ValueError("GitHub run start time has no timezone")
    return f"{started.astimezone(datetime.UTC):%Y%m%d%H%M}"


def _release_url(repository: str, tag: str, name: str) -> str:
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{urllib.parse.quote(tag, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}"
    )


def _public_name(record: Mapping[str, Any], role: str, tag: str) -> str:
    coordinate = record["coordinate"]
    stem = (
        f"{coordinate['package']}-{coordinate['source_version']}+{tag}-"
        f"{coordinate['target']}"
    )
    return f"{stem}{'-dbgsym' if role == 'dbgsym' else ''}.tar.zst"


def _snapshot(
    *,
    repository: str,
    tag: str,
    plan: Mapping[str, Any],
    records: Sequence[dict[str, Any]],
    started_at: str,
    environment: Mapping[str, str],
) -> tuple[dict[str, Any], bytes]:
    successful: list[dict[str, Any]] = []
    release_names = {CHECKSUM_NAME, SNAPSHOT_NAME}
    checksum_rows: list[tuple[str, str]] = []
    for record in records:
        files = []
        for source in record["files"]:
            item = dict(source)
            role = str(item["role"])
            if role in PUBLIC_ROLES:
                release_name = _public_name(record, role, tag)
                if release_name in release_names:
                    raise ValueError(
                        f"release-wide asset collision: {release_name}"
                    )
                release_names.add(release_name)
                item.update({
                    "release_name": release_name,
                    "url": _release_url(repository, tag, release_name),
                })
                checksum_rows.append((release_name, str(item["sha256"])))
            files.append(item)
        successful.append({**record, "files": files})
    checksum_bytes = "".join(
        f"{digest}  {name}\n" for name, digest in sorted(checksum_rows)
    ).encode()
    expected = sorted({
        (
            node_map(plan)[str(root["node_id"])]["package"],
            root["version"],
            root["target"],
        )
        for root in plan["roots"]
    })
    snapshot = {
        "checksum": {
            "media_type": "text/plain",
            "release_name": CHECKSUM_NAME,
            "sha256": hashlib.sha256(checksum_bytes).hexdigest(),
            "size": len(checksum_bytes),
            "url": _release_url(repository, tag, CHECKSUM_NAME),
        },
        "expected_coordinates": [
            {"package": item[0], "source_version": item[1], "target": item[2]}
            for item in expected
        ],
        "format": "ggbuild-snapshot-v1",
        "repository": repository,
        "run": {
            "attempt": environment["GITHUB_RUN_ATTEMPT"],
            "event": environment["GITHUB_EVENT_NAME"],
            "id": environment["GITHUB_RUN_ID"],
            "started_at": started_at,
            "workflow_ref": environment.get("GITHUB_WORKFLOW_REF", ""),
        },
        "source_commit": environment["GITHUB_SHA"],
        "successful": sorted(
            successful,
            key=lambda item: (
                item["coordinate"]["package"],
                item["coordinate"]["source_version"],
                item["coordinate"]["target"],
            ),
        ),
        "tag": tag,
    }
    return snapshot, checksum_bytes


def _release_body(snapshot: Mapping[str, Any]) -> str:
    versions = sorted({
        item["coordinate"]["source_version"] for item in snapshot["successful"]
    })
    targets = sorted({
        item["coordinate"]["target"] for item in snapshot["successful"]
    })
    run = snapshot["run"]
    repository = snapshot["repository"]
    tag = snapshot["tag"]
    lines = [
        "Complete PostgreSQL snapshot.",
        "",
        "PostgreSQL versions:",
        *(f"- {version}" for version in versions),
        "",
        "Targets:",
        *(f"- {target}" for target in targets),
        "",
        f"Source commit: {snapshot['source_commit']}",
        (
            f"Workflow run: https://github.com/{repository}/actions/runs/"
            f"{run['id']} (attempt {run['attempt']})"
        ),
        f"Checksums: {_release_url(repository, tag, CHECKSUM_NAME)}",
        f"Manifest: {_release_url(repository, tag, SNAPSHOT_NAME)}",
    ]
    return "\n".join(lines) + "\n"


# @lat: [[artifacts#Artifact Trust Pipeline#Complete Snapshot Publication]]
def publish_github(  # ruff: ignore[too-many-locals]
    plan: Mapping[str, Any],
    artifacts: pathlib.Path,
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    environment: Mapping[str, str] | None = None,
    client: GitHubPublisher | None = None,
) -> tuple[dict[str, Any], str]:
    publication = config.publication
    if publication is None:
        raise ValueError("project has no publication configuration")
    env = dict(os.environ if environment is None else environment)
    required = (
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_SHA",
        "GITHUB_EVENT_NAME",
    )
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise ValueError(f"missing GitHub environment: {', '.join(missing)}")
    if env["GITHUB_REPOSITORY"] != publication.repository:
        raise ValueError(
            "GitHub repository does not match publication configuration"
        )
    roots = {str(root["node_id"]): root for root in plan["roots"]}
    nodes = node_map(plan)
    records: list[dict[str, Any]] = []
    for path in sorted(artifacts.rglob(PUBLICATION_RECORD)):
        value = json.loads(path.read_text(encoding="utf-8"))
        node_id = value.get("test_node") if isinstance(value, dict) else None
        if not isinstance(node_id, str) or node_id not in roots:
            raise ValueError("downloaded publication is not a canonical root")
        records.append(
            _validate_record(value, path.parent, roots[node_id], nodes)
        )
    if len({item["test_node"] for item in records}) != len(records):
        raise ValueError("duplicate successful root publication")
    published_roots = {str(item["test_node"]) for item in records}
    expected_roots = set(roots)
    if published_roots != expected_roots:
        raise ValueError(
            "publication records do not exactly match planned roots; "
            f"missing={sorted(expected_roots - published_roots)}, "
            f"extra={sorted(published_roots - expected_roots)}"
        )
    github = client or GitHubClient(
        publication.repository, env.get("GITHUB_TOKEN", "")
    )
    started_at = github.run_started_at(env["GITHUB_RUN_ID"])
    tag = _tag(started_at)
    snapshot, checksum_bytes = _snapshot(
        repository=publication.repository,
        tag=tag,
        plan=plan,
        records=records,
        started_at=started_at,
        environment=env,
    )
    expected_coordinates = {
        (item["package"], item["source_version"], item["target"])
        for item in snapshot["expected_coordinates"]
    }
    successful_coordinates = {
        (
            item["coordinate"]["package"],
            item["coordinate"]["source_version"],
            item["coordinate"]["target"],
        )
        for item in snapshot["successful"]
    }
    if (
        len(successful_coordinates) != len(snapshot["successful"])
        or successful_coordinates != expected_coordinates
    ):
        raise ValueError(
            "successful coordinates do not exactly match expected coordinates"
        )
    if github.release_by_tag(tag) is not None:
        raise ValueError(f"GitHub release tag already exists: {tag}")
    if not dry_run:
        _publish_snapshot(github, snapshot, checksum_bytes, artifacts)
    return snapshot, tag


def _asset_bytes(client: GitHubPublisher, asset: Mapping[str, Any]) -> bytes:
    url = asset.get("url")
    if not isinstance(url, str):
        raise TypeError("GitHub release asset has no API URL")
    return bytes(
        client.request(
            "GET", url, headers={"Accept": "application/octet-stream"}
        ).content
    )


# @lat: [[artifacts#Artifact Trust Pipeline#Ordered Release Commit]]
def _publish_snapshot(
    client: GitHubPublisher,
    snapshot: Mapping[str, Any],
    checksum_bytes: bytes,
    artifacts: pathlib.Path,
) -> None:
    tag = str(snapshot["tag"])
    if client.release_by_tag(tag) is not None:
        raise ValueError(f"GitHub release tag already exists: {tag}")
    release = cast(
        "dict[str, Any]",
        client.request(
            "POST",
            f"{client.api}/releases",
            json={
                "body": _release_body(snapshot),
                "draft": True,
                "name": tag,
                "prerelease": False,
                "tag_name": tag,
                "target_commitish": snapshot["source_commit"],
            },
        ).json(),
    )
    upload_url = str(release["upload_url"]).partition("{")[0]
    record_dirs = {
        json.loads(path.read_text(encoding="utf-8"))["test_node"]: path.parent
        for path in artifacts.rglob(PUBLICATION_RECORD)
    }
    public: list[tuple[str, str, bytes]] = []
    for record in snapshot["successful"]:
        directory = record_dirs[record["test_node"]]
        public.extend(
            (
                item["release_name"],
                item["media_type"],
                (directory / item["logical_name"]).read_bytes(),
            )
            for item in record["files"]
            if item["role"] in PUBLIC_ROLES
        )
    for name, media_type, payload in sorted(public):
        client.request(
            "POST",
            upload_url,
            params={"name": name},
            headers={"Content-Type": media_type},
            data=payload,
        )
    checksum_asset = cast(
        "dict[str, Any]",
        client.request(
            "POST",
            upload_url,
            params={"name": CHECKSUM_NAME},
            headers={"Content-Type": "text/plain"},
            data=checksum_bytes,
        ).json(),
    )
    if _asset_bytes(client, checksum_asset) != checksum_bytes:
        raise ValueError("uploaded SHA256SUMS does not match local content")
    client.request(
        "POST",
        upload_url,
        params={"name": SNAPSHOT_NAME},
        headers={"Content-Type": "application/json"},
        data=canonical_json(snapshot).encode(),
    )
    client.request(
        "PATCH",
        f"{client.api}/releases/{release['id']}",
        json={"draft": False, "prerelease": False, "make_latest": "true"},
    )
