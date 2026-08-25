# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
import pathlib
import posixpath
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass

import zstandard

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PLAN_FORMAT_VERSION = 3
BUNDLE_FORMAT_VERSION = 2
_CHUNK_SIZE = 1024 * 1024
_CACHE_KEY_PREFIX = "ggbuild-v3-"


class BundleError(ValueError):
    """A dependency bundle is missing, corrupt, or incompatible."""


class NodeProvider(Protocol):
    def build_node(
        self, node: Mapping[str, Any], paths: ExecutionPaths
    ) -> Sequence[pathlib.Path]: ...


class PlanProvider(NodeProvider, Protocol):
    def create_plan(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ExecutionPaths:
    bundle_dir: pathlib.Path
    install_dir: pathlib.Path
    output_dir: pathlib.Path
    work_dir: pathlib.Path


NodeRole = Literal["bundle", "artifact", "test"]


@dataclass(frozen=True, slots=True)
class PlanNode:
    id: str
    role: NodeRole
    package: str
    recipe: str
    version: str
    target: str
    cache_key: str
    direct_dependencies: tuple[str, ...]
    runtime_dependencies: tuple[str, ...]
    build_dependencies: tuple[str, ...]
    artifact_dependencies: tuple[str, ...]
    bundle_install_subdir: str
    installation_path: str | None
    expected_outputs: tuple[Mapping[str, Any], ...]
    inputs: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PlanNode:
        role = value.get("role")
        if role not in {"bundle", "artifact", "test"}:
            raise ValueError(f"invalid node role: {role!r}")
        installation = value.get("installation_path")
        return cls(
            id=str(value["id"]),
            role=cast("NodeRole", role),
            package=str(value["package"]),
            recipe=str(value["recipe"]),
            version=str(value["version"]),
            target=str(value["target"]),
            cache_key=str(value["cache_key"]),
            direct_dependencies=tuple(value["direct_dependencies"]),
            runtime_dependencies=tuple(value["runtime_dependencies"]),
            build_dependencies=tuple(value["build_dependencies"]),
            artifact_dependencies=tuple(value["artifact_dependencies"]),
            bundle_install_subdir=str(value["bundle_install_subdir"]),
            installation_path=(str(installation) if installation else None),
            expected_outputs=tuple(value["expected_outputs"]),
            inputs=cast("Mapping[str, Any]", value["inputs"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_dependencies": list(self.artifact_dependencies),
            "build_dependencies": list(self.build_dependencies),
            "bundle_install_subdir": self.bundle_install_subdir,
            "cache_key": self.cache_key,
            "direct_dependencies": list(self.direct_dependencies),
            "expected_outputs": [
                dict(output) for output in self.expected_outputs
            ],
            "id": self.id,
            "inputs": dict(self.inputs),
            "installation_path": self.installation_path,
            "package": self.package,
            "recipe": self.recipe,
            "role": self.role,
            "runtime_dependencies": list(self.runtime_dependencies),
            "target": self.target,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class BuildPlan:
    project: str
    targets: tuple[str, ...]
    nodes: tuple[PlanNode, ...]
    layers: tuple[tuple[str, ...], ...]
    roots: tuple[Mapping[str, Any], ...]
    build_options: Mapping[str, Any]
    resolved_packages: Mapping[str, Any]
    format_version: int = PLAN_FORMAT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "build_options": dict(self.build_options),
            "format_version": self.format_version,
            "layers": [list(layer) for layer in self.layers],
            "nodes": [node.as_dict() for node in self.nodes],
            "project": self.project,
            "resolved_packages": dict(self.resolved_packages),
            "roots": [dict(root) for root in self.roots],
            "targets": list(self.targets),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BuildPlan:
        validate_plan(value)
        return cls(
            project=str(value["project"]),
            targets=tuple(value["targets"]),
            nodes=tuple(PlanNode.from_mapping(node) for node in value["nodes"]),
            layers=tuple(tuple(layer) for layer in value["layers"]),
            roots=tuple(value["roots"]),
            build_options=cast("Mapping[str, Any]", value["build_options"]),
            resolved_packages=cast(
                "Mapping[str, Any]", value["resolved_packages"]
            ),
        )


def canonical_json(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    )


def digest_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def tree_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    files = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in files:
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.name if root.is_file() else path.relative_to(root)
        digest.update(str(relative).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git(root: pathlib.Path, *args: str) -> bytes | None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [  # ruff: ignore[start-process-with-partial-path]
            "git",
            "-C",
            str(root),
            *args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def source_identity(
    distribution: str,
    module: str,
    *,
    relevant_roots: Sequence[pathlib.Path] = (),
) -> str:
    """Fingerprint a release or editable checkout including all dirty files."""
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = "0+unknown"
    spec = importlib.util.find_spec(module)
    locations = list(spec.submodule_search_locations or ()) if spec else []
    module_root = (
        pathlib.Path(locations[0])
        if locations
        else pathlib.Path(spec.origin).parent
        if spec and spec.origin
        else None
    )
    if module_root is None:
        return f"release:{version}"
    root_bytes = _git(module_root, "rev-parse", "--show-toplevel")
    if root_bytes is None:
        return f"release:{version}"
    checkout = pathlib.Path(root_bytes.decode().strip())
    head_bytes = _git(checkout, "rev-parse", "HEAD")
    head = head_bytes.decode().strip() if head_bytes else "unknown"
    roots = tuple(relevant_roots) or (module_root,)
    content = hashlib.sha256()
    for root in sorted(roots):
        content.update(str(root.name).encode())
        content.update(tree_digest(root).encode())
    return f"editable:{version}:{head}:{content.hexdigest()}"


def topological_layers(
    dependencies: Mapping[str, Sequence[str]],
) -> list[list[str]]:
    remaining = {node: set(edges) for node, edges in dependencies.items()}
    layers: list[list[str]] = []
    while remaining:
        layer = sorted(node for node, edges in remaining.items() if not edges)
        if not layer:
            raise ValueError("build plan contains a dependency cycle")
        layers.append(layer)
        for node in layer:
            del remaining[node]
        for edges in remaining.values():
            edges.difference_update(layer)
    return layers


def validate_plan(  # ruff: ignore[too-many-branches, too-many-locals, too-many-statements]
    plan: Mapping[str, Any],
) -> None:
    if plan.get("format_version") != PLAN_FORMAT_VERSION:
        raise ValueError("unsupported build plan format; only v3 is accepted")
    if not isinstance(plan.get("project"), str) or not plan["project"]:
        raise ValueError("build plan project must be a non-empty string")
    raw_targets = plan.get("targets")
    if (
        not isinstance(raw_targets, list)
        or not raw_targets
        or not all(isinstance(target, str) and target for target in raw_targets)
    ):
        raise ValueError("build plan targets must be sorted and unique")
    targets = cast("list[str]", raw_targets)
    if targets != sorted(set(targets)):
        raise ValueError("build plan targets must be sorted and unique")
    if not isinstance(plan.get("build_options"), dict):
        raise TypeError("build plan build_options must be an object")
    if not isinstance(plan.get("resolved_packages"), dict):
        raise TypeError("build plan resolved_packages must be an object")
    raw_node_values = plan.get("nodes")
    if not isinstance(raw_node_values, list):
        raise TypeError("build plan nodes must be a list")
    if not all(isinstance(node, dict) for node in raw_node_values):
        raise TypeError("build plan nodes must be objects")
    raw_nodes = cast("list[dict[str, Any]]", raw_node_values)
    raw_ids = [node.get("id") for node in raw_nodes]
    if any(not isinstance(node_id, str) or not node_id for node_id in raw_ids):
        raise ValueError("build plan has an invalid node id")
    ids = cast("list[str]", raw_ids)
    if len(ids) != len(set(ids)):
        raise ValueError("build plan has duplicate node ids")
    if ids != sorted(ids):
        raise ValueError("build plan nodes must be sorted by id")
    known: set[str] = {node_id for node_id in ids if isinstance(node_id, str)}
    graph: dict[str, list[str]] = {}
    for node in raw_nodes:
        node_id = node["id"]
        for field in (
            "package",
            "recipe",
            "version",
            "target",
            "bundle_install_subdir",
        ):
            if not isinstance(node.get(field), str) or not node[field]:
                raise ValueError(f"invalid {field} for {node_id}")
        if node["target"] not in targets:
            raise ValueError(f"unknown target for {node_id}: {node['target']}")
        role = node.get("role")
        if role not in {"bundle", "artifact", "test"}:
            raise ValueError(f"invalid role for {node_id}: {role!r}")
        cache_key = node.get("cache_key")
        if (
            not isinstance(cache_key, str)
            or not cache_key.startswith(_CACHE_KEY_PREFIX)
            or len(cache_key) != len(_CACHE_KEY_PREFIX) + 64
            or any(
                character not in "0123456789abcdef"
                for character in cache_key.removeprefix(_CACHE_KEY_PREFIX)
            )
        ):
            raise ValueError(f"invalid cache key for {node_id}")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise TypeError(f"invalid inputs for {node_id}")
        expected_key = _CACHE_KEY_PREFIX + digest_json(inputs)
        if cache_key != expected_key:
            raise ValueError(f"stale cache key for {node_id}")
        direct = node.get("direct_dependencies")
        if not isinstance(direct, list) or not all(
            isinstance(item, str) for item in direct
        ):
            raise ValueError(f"invalid dependencies for {node_id}")
        direct = cast("list[str]", direct)
        if direct != sorted(set(direct)):
            raise ValueError(
                f"dependencies for {node_id} must be sorted and unique"
            )
        dependency_ids = {item for item in direct if isinstance(item, str)}
        if missing := dependency_ids - known:
            raise ValueError(
                f"unknown dependencies for {node_id}: {sorted(missing)}"
            )
        if node_id in dependency_ids:
            raise ValueError(f"node {node_id} depends on itself")
        runtime = node.get("runtime_dependencies")
        build = node.get("build_dependencies")
        artifacts = node.get("artifact_dependencies")
        if not all(
            isinstance(value, list) for value in (runtime, build, artifacts)
        ):
            raise TypeError(f"dependency roles for {node_id} must be lists")
        runtime_values = cast("list[object]", runtime)
        build_values = cast("list[object]", build)
        artifact_values = cast("list[object]", artifacts)
        dependency_roles_raw = runtime_values + build_values + artifact_values
        if not all(isinstance(item, str) for item in dependency_roles_raw):
            raise TypeError(f"dependency roles for {node_id} must be strings")
        runtime = cast("list[str]", runtime)
        build = cast("list[str]", build)
        artifacts = cast("list[str]", artifacts)
        if (
            runtime != sorted(set(runtime))
            or build != sorted(set(build))
            or artifacts != sorted(set(artifacts))
            or set(runtime) | set(build) | set(artifacts) != dependency_ids
            or (set(runtime) | set(build) | set(artifacts)) - known
        ):
            raise ValueError(f"invalid dependency roles for {node_id}")
        dependency_roles = {
            dependency_id: next(
                candidate["role"]
                for candidate in raw_nodes
                if candidate["id"] == dependency_id
            )
            for dependency_id in dependency_ids
        }
        if any(dependency_roles[item] != "artifact" for item in artifacts):
            raise ValueError(f"invalid artifact dependencies for {node_id}")
        if role == "test":
            if len(artifacts) != 1 or runtime or build:
                raise ValueError(f"invalid test dependencies for {node_id}")
        elif artifacts:
            raise ValueError(f"invalid artifact dependencies for {node_id}")
        installation_path = node.get("installation_path")
        if role == "bundle":
            if not isinstance(installation_path, str) or not installation_path:
                raise ValueError(
                    f"bundle node {node_id} needs installation_path"
                )
            path = pathlib.PurePosixPath(installation_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe installation_path for {node_id}")
        elif installation_path is not None:
            raise ValueError(
                f"{role} node {node_id} cannot have installation_path"
            )
        outputs = node.get("expected_outputs")
        if not isinstance(outputs, list) or not outputs:
            raise ValueError(f"node {node_id} needs expected outputs")
        for output in outputs:
            if not isinstance(output, dict):
                raise TypeError(f"invalid expected output for {node_id}")
            output_path = output.get("path")
            if not isinstance(output_path, str) or not output_path:
                raise ValueError(f"invalid expected output path for {node_id}")
            path = pathlib.PurePosixPath(output_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe expected output path for {node_id}")
        graph[node_id] = direct
    if plan.get("layers") != topological_layers(graph):
        raise ValueError("build plan has stale topological layers")
    roots = plan.get("roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("build plan needs root executions")
    artifact_ids = {
        node["id"] for node in raw_nodes if node.get("role") == "artifact"
    }
    test_ids = {node["id"] for node in raw_nodes if node.get("role") == "test"}
    expected_root_ids = test_ids | {
        artifact_id
        for artifact_id in artifact_ids
        if not any(
            artifact_id in node.get("artifact_dependencies", [])
            for node in raw_nodes
        )
    }
    root_ids: list[str] = []
    for root in roots:
        if not isinstance(root, dict):
            raise TypeError("build plan roots must be objects")
        node_id = root.get("node_id")
        if node_id not in expected_root_ids:
            raise ValueError(f"invalid root node: {node_id!r}")
        if root.get("target") not in targets or not isinstance(
            root.get("version"), str
        ):
            raise ValueError(f"invalid root execution: {node_id}")
        closure = root.get("closure")
        if not isinstance(closure, list) or not all(
            isinstance(item, str) for item in closure
        ):
            raise ValueError(f"invalid root closure: {node_id}")
        closure = cast("list[str]", closure)
        if (
            closure != sorted(set(closure))
            or set(closure) - known
            or node_id not in closure
        ):
            raise ValueError(f"invalid root closure: {node_id}")
        closure_digest = root.get("closure_digest")
        expected_digest = digest_json([
            next(node["cache_key"] for node in raw_nodes if node["id"] == item)
            for item in closure
        ])
        if closure_digest != expected_digest:
            raise ValueError(f"stale closure digest for {node_id}")
        root_ids.append(cast("str", node_id))
    if sorted(root_ids) != sorted(expected_root_ids) or len(root_ids) != len(
        set(root_ids)
    ):
        raise ValueError("build plan roots do not match artifact/test nodes")


def load_plan(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("build plan root must be an object")
    validate_plan(value)
    return value


def write_plan(path: pathlib.Path, plan: Mapping[str, Any]) -> None:
    validate_plan(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as target:
            target.write(canonical_json(plan))
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def node_map(plan: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in plan["nodes"]}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_manifest(
    root: pathlib.Path,
    include: set[str] | None = None,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if include is not None and relative not in include:
            continue
        item: dict[str, Any] = {"path": relative}
        if path.is_symlink():
            item |= {
                "type": "symlink",
                "target": path.readlink().as_posix(),
            }
        elif path.is_file():
            item |= {
                "mode": stat.S_IMODE(path.stat().st_mode),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "type": "file",
            }
        elif path.is_dir():
            item |= {
                "mode": stat.S_IMODE(path.stat().st_mode),
                "type": "directory",
            }
        else:
            raise BundleError(f"unsupported staged file type: {path}")
        files.append(item)
    return files


def _tar_info(
    name: str, *, size: int = 0, mode: int = 0o644
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, mode, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add_tree(
    archive: tarfile.TarFile,
    root: pathlib.Path,
    include: set[str] | None = None,
) -> None:
    tree = _tar_info("tree/", mode=stat.S_IMODE(root.stat().st_mode))
    tree.type = tarfile.DIRTYPE
    archive.addfile(tree)
    for path in sorted(root.rglob("*")):
        path_relative = path.relative_to(root).as_posix()
        if include is not None and path_relative not in include:
            continue
        relative = pathlib.PurePosixPath("tree") / path_relative
        if path.is_symlink():
            info = _tar_info(str(relative), mode=0o777)
            info.type = tarfile.SYMTYPE
            info.linkname = path.readlink().as_posix()
            archive.addfile(info)
        elif path.is_dir():
            info = _tar_info(
                f"{relative}/", mode=stat.S_IMODE(path.stat().st_mode)
            )
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        elif path.is_file():
            info = _tar_info(
                str(relative),
                size=path.stat().st_size,
                mode=stat.S_IMODE(path.stat().st_mode),
            )
            with path.open("rb") as source:
                archive.addfile(info, source)


def bundle_path(cache_dir: pathlib.Path, cache_key: str) -> pathlib.Path:
    return cache_dir / "bundles" / f"{cache_key}.tar.zst"


def export_bundle(
    staging_root: pathlib.Path,
    destination: pathlib.Path,
    *,
    node: Mapping[str, Any],
    baseline: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if node.get("role") != "bundle" or not staging_root.is_dir():
        raise BundleError("bundle staged installation is missing")
    current = _file_manifest(staging_root)
    baseline_paths = {str(item["path"]) for item in baseline}
    include = {
        str(item["path"])
        for item in current
        if str(item["path"]) not in baseline_paths
    }
    for path in tuple(include):
        parent = pathlib.PurePosixPath(path).parent
        while parent.parts:
            include.add(parent.as_posix())
            parent = parent.parent
    manifest = {
        "cache_key": node["cache_key"],
        "files": _file_manifest(staging_root, include),
        "format_version": BUNDLE_FORMAT_VERSION,
        "installation_path": node["installation_path"],
        "node_id": node["id"],
        "package": node["package"],
        "target": node["target"],
        "version": node["version"],
    }
    payload = canonical_json(manifest).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with tempfile.TemporaryFile() as tar_stream:
            with tarfile.open(fileobj=tar_stream, mode="w") as archive:
                archive.addfile(
                    _tar_info("manifest.json", size=len(payload)),
                    io.BytesIO(payload),
                )
                _add_tree(archive, staging_root, include)
            tar_stream.seek(0)
            with temporary.open("wb") as compressed:
                zstandard.ZstdCompressor(level=19).copy_stream(
                    tar_stream, compressed
                )
                compressed.flush()
                os.fsync(compressed.fileno())
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return manifest


def _validate_manifest(
    manifest: Mapping[str, Any],
    node: Mapping[str, Any],
    *,
    allow_cache_key_mismatch: bool = False,
) -> None:
    expected = {
        "cache_key": node["cache_key"],
        "format_version": BUNDLE_FORMAT_VERSION,
        "installation_path": node["installation_path"],
        "node_id": node["id"],
        "package": node["package"],
        "target": node["target"],
        "version": node["version"],
    }
    mismatches = [
        key
        for key, value in expected.items()
        if manifest.get(key) != value
        and not (allow_cache_key_mismatch and key == "cache_key")
    ]
    if mismatches:
        raise BundleError(
            "incompatible bundle manifest fields: " + ", ".join(mismatches)
        )
    if not isinstance(manifest.get("files"), list):
        raise BundleError("bundle manifest file list is invalid")
    for item in manifest["files"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BundleError("bundle manifest file entry is invalid")
        path = pathlib.PurePosixPath(item["path"])
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise BundleError(f"unsafe path in bundle manifest: {path}")
        if item.get("type") == "symlink":
            target = item.get("target")
            if not isinstance(target, str):
                raise BundleError(f"invalid symlink target for {path}")
            if not _safe_symlink_target(path, target):
                raise BundleError(f"unsafe symlink target for {path}: {target}")


def _safe_symlink_target(path: pathlib.PurePosixPath, target: str) -> bool:
    windows_link = pathlib.PureWindowsPath(target)
    normalized_target = target.replace("\\", "/")
    link = pathlib.PurePosixPath(normalized_target)
    if windows_link.drive or windows_link.root or link.is_absolute():
        return False
    resolved = pathlib.PurePosixPath(
        posixpath.normpath((path.parent / link).as_posix())
    )
    return not resolved.is_absolute() and ".." not in resolved.parts


def _decompress_bundle(bundle: pathlib.Path, destination: pathlib.Path) -> None:
    try:
        with bundle.open("rb") as source, destination.open("wb") as target:
            zstandard.ZstdDecompressor().copy_stream(source, target)
    except zstandard.ZstdError as error:
        raise BundleError(f"corrupt zstd bundle: {bundle}") from error


def _safe_tree_members(
    members: Sequence[tarfile.TarInfo],
) -> list[tarfile.TarInfo]:
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.islnk():
            raise BundleError(f"unsafe path in bundle: {member.name}")
        if member.issym() and not _safe_symlink_target(path, member.linkname):
            raise BundleError(f"unsafe symlink in bundle: {member.name}")
    return [member for member in members if member.name.startswith("tree/")]


def _bundle_manifest(
    archive: tarfile.TarFile,
    node: Mapping[str, Any],
    *,
    allow_cache_key_mismatch: bool = False,
) -> dict[str, Any]:
    manifest_source = archive.extractfile("manifest.json")
    if manifest_source is None:
        raise BundleError("bundle manifest is missing")
    manifest = json.load(manifest_source)
    if not isinstance(manifest, dict):
        raise BundleError("bundle manifest must be an object")
    _validate_manifest(
        manifest,
        node,
        allow_cache_key_mismatch=allow_cache_key_mismatch,
    )
    return manifest


def _extract_bundle(
    raw: pathlib.Path,
    temporary_root: pathlib.Path,
    node: Mapping[str, Any],
    *,
    allow_cache_key_mismatch: bool = False,
) -> dict[str, Any]:
    try:
        with tarfile.open(raw, mode="r:") as archive:
            tree_members = _safe_tree_members(archive.getmembers())
            manifest = _bundle_manifest(
                archive,
                node,
                allow_cache_key_mismatch=allow_cache_key_mismatch,
            )
            archive.extractall(
                temporary_root, members=tree_members, filter="data"
            )
    except (tarfile.TarError, json.JSONDecodeError) as error:
        raise BundleError(f"corrupt dependency bundle: {raw}") from error
    return manifest


def restore_bundle(
    bundle: pathlib.Path,
    installation_root: pathlib.Path,
    *,
    node: Mapping[str, Any],
    allow_cache_key_mismatch: bool = False,
) -> dict[str, Any]:
    if not bundle.is_file():
        raise BundleError(f"dependency bundle is missing: {bundle}")
    installation_root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f".{installation_root.name}.",
            dir=installation_root.parent,
        )
    )
    try:
        with tempfile.TemporaryDirectory() as raw_directory:
            raw = pathlib.Path(raw_directory) / "bundle.tar"
            _decompress_bundle(bundle, raw)
            manifest = _extract_bundle(
                raw,
                temporary_root,
                node,
                allow_cache_key_mismatch=allow_cache_key_mismatch,
            )
        tree = temporary_root / "tree"
        if not tree.is_dir() or _file_manifest(tree) != manifest["files"]:
            raise BundleError("bundle contents do not match its manifest")
        publish = temporary_root.with_name(f"{temporary_root.name}.publish")
        tree.replace(publish)
        if installation_root.exists():
            shutil.rmtree(installation_root)
        publish.replace(installation_root)
        return manifest
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def merge_bundle(
    bundle: pathlib.Path,
    installation_root: pathlib.Path,
    *,
    node: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate a bundle and merge its immutable sysroot into a node sysroot."""
    with tempfile.TemporaryDirectory(
        prefix=f".{node['package']}.", dir=installation_root.parent
    ) as temporary:
        restored = pathlib.Path(temporary) / "tree"
        manifest = restore_bundle(bundle, restored, node=node)
        _merge_tree(restored, installation_root)
        return manifest


def _merge_tree(source: pathlib.Path, destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            link = path.readlink()
            if target.is_symlink() and target.readlink() == link:
                continue
            if target.exists() or target.is_symlink():
                raise BundleError(f"conflicting dependency path: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link)
        elif path.is_dir():
            if target.is_symlink():
                raise BundleError(f"conflicting dependency path: {relative}")
            if target.exists() and not target.is_dir():
                raise BundleError(f"conflicting dependency path: {relative}")
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            if target.is_file():
                if sha256_file(path) == sha256_file(target):
                    continue
                raise BundleError(f"conflicting dependency path: {relative}")
            if target.exists() or target.is_symlink():
                raise BundleError(f"conflicting dependency path: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def execute_node(
    plan_path: pathlib.Path,
    node_id: str,
    paths: ExecutionPaths,
    provider: NodeProvider,
) -> tuple[pathlib.Path, ...]:
    plan = load_plan(plan_path)
    nodes = node_map(plan)
    if node_id not in nodes:
        raise ValueError(f"unknown build plan node: {node_id}")
    node = nodes[node_id]
    paths.work_dir.mkdir(parents=True, exist_ok=True)
    dependency_ids: list[str] = []
    seen: set[str] = set()

    def add_dependencies(current_id: str) -> None:
        for dependency_id in nodes[current_id]["direct_dependencies"]:
            if dependency_id in seen:
                continue
            if nodes[dependency_id]["role"] != "artifact":
                add_dependencies(dependency_id)
            seen.add(dependency_id)
            dependency_ids.append(dependency_id)

    add_dependencies(node_id)
    installation = paths.work_dir / "sysroot"
    installation.mkdir(parents=True, exist_ok=True)
    for dependency_id in dependency_ids:
        dependency = nodes[dependency_id]
        if dependency["role"] != "bundle":
            continue
        merge_bundle(
            bundle_path(paths.bundle_dir, dependency["cache_key"]),
            installation,
            node=dependency,
        )
    dependency_baseline = _file_manifest(installation)
    dependency_manifest = paths.work_dir / "dependency-nodes.json"
    dependency_manifest.write_text(
        canonical_json([
            nodes[dependency_id] for dependency_id in dependency_ids
        ]),
        encoding="utf-8",
    )
    outputs = tuple(
        pathlib.Path(path) for path in provider.build_node(node, paths)
    )
    if node["role"] == "bundle":
        if len(outputs) != 1 or not outputs[0].is_dir():
            raise RuntimeError(
                "bundle node provider must return one staged installation tree"
            )
        output = bundle_path(paths.bundle_dir, node["cache_key"])
        export_bundle(
            outputs[0],
            output,
            node=node,
            baseline=dependency_baseline,
        )
        return (output,)
    return outputs
