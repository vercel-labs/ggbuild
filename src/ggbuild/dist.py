# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Package distribution and editable-checkout identity utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dataclasses
import functools
import hashlib
import importlib.metadata
import json
import os
import pathlib
import re
import subprocess
import urllib.parse

if TYPE_CHECKING:
    from collections.abc import Sequence


_PROJECT_DIST_NAME = "ggbuild"
_IGNORE_DIRTY_ENV = "GGBUILD_IGNORE_DIRTY_STATE"


@dataclasses.dataclass(kw_only=True, frozen=True)
class DirectURLOrigin:
    url: str
    editable: bool = False
    commit_id: str | None = None


@functools.cache
def get_direct_url_origin(
    dist_name: str,
    path: Sequence[str] | None = None,
) -> DirectURLOrigin | None:
    """Return PEP 610/660 direct URL metadata for a package, if present."""
    distributions = (
        importlib.metadata.distributions(name=dist_name, path=[*path])
        if path is not None
        else importlib.metadata.distributions(name=dist_name)
    )
    for distribution in distributions:
        origin = _get_direct_url_origin(distribution)
        if origin is not None:
            return origin
    return None


@functools.cache
def get_dist_version(
    dist_name: str,
    path: Sequence[str] | None = None,
) -> str | None:
    distributions = (
        importlib.metadata.distributions(name=dist_name, path=[*path])
        if path is not None
        else importlib.metadata.distributions(name=dist_name)
    )
    for distribution in distributions:
        return distribution.version
    return None


def _get_direct_url_origin(
    distribution: importlib.metadata.Distribution,
) -> DirectURLOrigin | None:
    try:
        data = distribution.read_text("direct_url.json")
    except OSError:
        return None
    if data is None:
        return None
    try:
        info = json.loads(data)
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    info = cast("dict[str, Any]", info)
    url = info.get("url")
    if not isinstance(url, str) or not url:
        return None
    directory = info.get("dir_info")
    editable = (
        bool(cast("dict[str, Any]", directory).get("editable", False))
        if isinstance(directory, dict)
        else False
    )
    vcs = info.get("vcs_info")
    commit_id = (
        cast("dict[str, Any]", vcs).get("commit_id")
        if isinstance(vcs, dict)
        else None
    )
    return DirectURLOrigin(
        url=url,
        editable=editable,
        commit_id=commit_id if isinstance(commit_id, str) else None,
    )


def get_origin_source_dir(dist_name: str) -> pathlib.Path | None:
    origin = get_direct_url_origin(dist_name)
    if origin is None:
        return None
    try:
        parsed = urllib.parse.urlparse(origin.url)
    except ValueError:
        return None
    if parsed.scheme != "file" or not parsed.path:
        return None
    path = pathlib.Path(urllib.parse.unquote(parsed.path))
    return path.resolve() if path.is_dir() else None


def _git(source_dir: pathlib.Path, *arguments: str) -> bytes | None:
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            ["git", *arguments],  # ruff: ignore[start-process-with-partial-path]
            check=True,
            cwd=source_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError, subprocess.CalledProcessError:
        return None
    return result.stdout


def _is_revision_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{5,40}", value))


def get_origin_commit_id(dist_name: str) -> str | None:
    origin = get_direct_url_origin(dist_name)
    if origin is None:
        return None
    if origin.commit_id is not None:
        return origin.commit_id
    source_dir = get_origin_source_dir(dist_name)
    if source_dir is None:
        return None
    output = _git(source_dir, "rev-parse", "HEAD")
    if output is None:
        return None
    commit_id = output.decode().strip()
    return commit_id if _is_revision_sha(commit_id) else None


def ignore_dirty_state() -> bool:
    raw = os.environ.get(_IGNORE_DIRTY_ENV)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{_IGNORE_DIRTY_ENV} must be a boolean value, got {raw!r}"
    )


def dirty_tree_digest(source_dir: pathlib.Path) -> str | None:
    """Hash tracked changes and untracked, non-ignored files in a checkout."""
    diff = _git(source_dir, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    untracked = _git(
        source_dir,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if diff is None or untracked is None:
        return None
    paths = sorted(path for path in untracked.split(b"\0") if path)
    if not diff and not paths:
        return None
    digest = hashlib.sha256()
    digest.update(diff)
    digest.update(b"\0")
    for encoded in paths:
        relative = pathlib.PurePosixPath(encoded.decode())
        path = source_dir / pathlib.Path(*relative.parts)
        digest.update(encoded)
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(str(path.readlink()).encode())
        elif path.is_file():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


@functools.cache
def get_project_version_key() -> str:
    """Return a version, revision, and optional dirty-state identity."""
    version_key = get_dist_version(_PROJECT_DIST_NAME) or "0+unknown"
    commit_id = get_origin_commit_id(_PROJECT_DIST_NAME)
    if commit_id:
        version_key = f"{version_key}.dev{commit_id[:9]}"
    if not ignore_dirty_state():
        source_dir = get_origin_source_dir(_PROJECT_DIST_NAME)
        dirty = (
            dirty_tree_digest(source_dir) if source_dir is not None else None
        )
        if dirty:
            version_key = f"{version_key}.dirty{dirty[:16]}"
    return version_key
