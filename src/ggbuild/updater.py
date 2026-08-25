# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Generic, atomic registered-release updates."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    ClassVar,
    Literal,
    NotRequired,
    TypedDict,
    cast,
)

import dataclasses
import hashlib
import inspect
import json
import os
import pathlib
import re
import tempfile
import urllib.parse
import urllib.request

from packaging.version import Version

import ggbuild.targets as _targets  # ruff: ignore[unused-import] - init order
from ggbuild import packages, patches as package_patches
from ggbuild.packages import repository

if TYPE_CHECKING:
    from cleo.io import io as cleo_io


class UpdatePolicy(TypedDict):
    type: Literal["github-release", "html-index"]
    repository: NotRequired[str]
    tag: NotRequired[str]
    major: NotRequired[str]
    url: NotRequired[str]
    pattern: NotRequired[str]


class UpdateablePackage:
    update_policy: ClassVar[UpdatePolicy]

    @classmethod
    def discover_releases(cls) -> tuple[str, ...]:
        """Return the complete desired registered-release set."""
        return (latest_version(cls.update_policy),)


class UpdateableBundledCAutoconfPackage(
    UpdateablePackage, packages.BundledCAutoconfPackage
):
    pass


class UpdateableBundledCMakePackage(
    UpdateablePackage, packages.BundledCMakePackage
):
    pass


class UpdateableBundledCPackage(UpdateablePackage, packages.BundledCPackage):
    pass


USER_AGENT = "ggbuild-updater/2"
_DECLARATION = re.compile(
    r"(?ms)^(?P<name>[A-Za-z][A-Za-z0-9_]*)\(\s*"
    r'"(?P<version>[^"]+)",\s*sha256="(?P<sha256>[0-9a-f]{64})"'
    r"\s*,?\s*\)$"
)


def fetch(url: str) -> bytes:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing non-HTTPS URL: {url}")
    request = urllib.request.Request(  # ruff: ignore[suspicious-url-open-usage]
        url, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # ruff: ignore[suspicious-url-open-usage]
        return cast("bytes", response.read())


def latest_version(policy: UpdatePolicy) -> str:
    if policy["type"] == "github-release":
        repository_name = policy.get("repository")
        tag_template = policy.get("tag")
        if not repository_name or not tag_template:
            raise ValueError("github-release policy needs repository and tag")
        if major := policy.get("major"):
            url = (
                f"https://api.github.com/repos/{repository_name}"
                "/releases?per_page=100"
            )
            payload = json.loads(fetch(url))
            tags = [item["tag_name"] for item in payload]
            pattern = re.escape(tag_template).replace(
                re.escape("{version}"), rf"(?P<version>{re.escape(major)}\..+)"
            )
            versions = {
                Version(match.group("version"))
                for tag in tags
                if (match := re.fullmatch(pattern, tag)) is not None
                and not Version(match.group("version")).is_prerelease
            }
            if not versions:
                raise ValueError(f"no major {major} releases found at {url}")
            return str(max(versions))
        url = f"https://api.github.com/repos/{repository_name}/releases/latest"
        tag = cast("str", json.loads(fetch(url))["tag_name"])
        pattern = re.escape(tag_template).replace(
            re.escape("{version}"), r"(?P<version>.+)"
        )
        match = re.fullmatch(pattern, tag)
        if match is None:
            raise ValueError(f"unexpected latest tag {tag!r} for {url}")
        version = Version(match.group("version"))
        if version.is_prerelease:
            raise ValueError(f"latest release is not stable: {version}")
        return str(version)
    if policy["type"] == "html-index":
        index_url = policy.get("url")
        index_pattern = policy.get("pattern")
        if not index_url or not index_pattern:
            raise ValueError("html-index policy needs url and pattern")
        text = fetch(index_url).decode("utf-8", "replace")
        versions = {Version(item) for item in re.findall(index_pattern, text)}
        stable = [version for version in versions if not version.is_prerelease]
        if not stable:
            raise ValueError(f"no stable versions found at {index_url}")
        return str(max(stable))
    raise ValueError(f"unknown update policy: {policy['type']}")


def registered_releases() -> tuple[packages.BundledPackage, ...]:
    releases = [
        package
        for package in repository.bundle_repo.packages
        if isinstance(package, packages.BundledPackage)
        and isinstance(package, UpdateablePackage)
        and package.sha256 is not None
    ]
    return tuple(
        sorted(releases, key=lambda item: (str(item.name), item.version))
    )


def source_url(package: packages.BundledPackage, version: str) -> str:
    sources = type(package)._get_sources(version)  # ruff: ignore[private-member-access]
    if len(sources) != 1:
        raise ValueError(f"{package.name} does not have one archive source")
    return sources[0].url


def declaration(name: str, version: str, sha256: str) -> str:
    return f'{name}(\n    "{version}",\n    sha256="{sha256}",\n)'


def rewrite_declaration(
    package: packages.BundledPackage,
    version: str,
    sha256: str,
    texts: dict[pathlib.Path, str],
) -> None:
    source_file = inspect.getsourcefile(type(package))
    if source_file is None:
        raise ValueError(f"source file not found for {type(package)!r}")
    path = pathlib.Path(source_file)
    text = texts.setdefault(path, path.read_text(encoding="utf-8"))
    class_name = type(package).__name__
    matches = [
        match
        for match in _DECLARATION.finditer(text)
        if match.group("name") == class_name
        and match.group("version") == package.source_version
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one standardized {class_name} release in {path}"
        )
    match = matches[0]
    texts[path] = (
        text[: match.start()]
        + declaration(class_name, version, sha256)
        + text[match.end() :]
    )


def rewrite_declarations(
    recipe: type[packages.BundledPackage],
    releases: dict[str, str],
    texts: dict[pathlib.Path, str],
) -> None:
    """Replace all standardized release declarations for *recipe*."""
    source_file = inspect.getsourcefile(recipe)
    if source_file is None:
        raise ValueError(f"source file not found for {recipe!r}")
    path = pathlib.Path(source_file)
    text = texts.setdefault(path, path.read_text(encoding="utf-8"))
    matches = [
        match
        for match in _DECLARATION.finditer(text)
        if match.group("name") == recipe.__name__
    ]
    if not matches:
        raise ValueError(
            f"expected standardized {recipe.__name__} releases in {path}"
        )
    blocks = [
        declaration(recipe.__name__, version, releases[version])
        for version in sorted(releases, key=Version)
    ]
    texts[path] = (
        text[: matches[0].start()]
        + "\n\n".join(blocks)
        + text[matches[-1].end() :]
    )


def write_atomic(path: pathlib.Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_transaction(texts: dict[pathlib.Path, str]) -> None:
    """Replace text files as one best-effort filesystem transaction."""
    originals = {
        path: path.read_bytes() if path.exists() else None for path in texts
    }
    replaced: list[pathlib.Path] = []
    try:
        for path, text in texts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, text)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
                continue
            descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(descriptor, "wb") as target:
                target.write(original)
                target.flush()
                os.fsync(target.fileno())
            pathlib.Path(temporary_name).replace(path)
        raise


@dataclasses.dataclass(frozen=True, slots=True)
class ReleaseChange:
    package: str
    previous: tuple[str, ...]
    current: tuple[str, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class UpdateResult:
    releases: tuple[ReleaseChange, ...]
    patches: tuple[package_patches.PatchReroll, ...]


def _desired_releases(
    recipe: type[packages.BundledPackage],
    package: packages.BundledPackage,
) -> tuple[str, ...]:
    if not issubclass(recipe, UpdateablePackage):
        raise TypeError(f"{package.name} is not updateable")
    desired = tuple(sorted(set(recipe.discover_releases()), key=Version))
    if not desired:
        raise ValueError(f"{package.name} release discovery returned nothing")
    return desired


def _candidate_releases(
    recipe: type[packages.BundledPackage],
    instances: list[packages.BundledPackage],
    desired: tuple[str, ...],
    io: cleo_io.IO,
) -> tuple[dict[str, str], list[packages.BundledPackage]]:
    old_hashes = {
        item.source_version: item.sha256
        for item in instances
        if item.sha256 is not None
    }
    hashes: dict[str, str] = {}
    candidates: list[packages.BundledPackage] = []
    for version in desired:
        if version in old_hashes:
            hashes[version] = old_hashes[version]
            continue
        sources = recipe._get_sources(version)  # ruff: ignore[private-member-access]
        if len(sources) != 1 or not isinstance(
            sources[0], packages.HttpsSource
        ):
            raise ValueError(
                f"{instances[-1].name} does not have one HTTPS archive source"
            )
        archive = sources[0].download(io)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        hashes[version] = digest
        candidates.append(recipe(version, sha256=digest))
    return hashes, candidates


def _collect_patch_updates(
    candidates: list[packages.BundledPackage],
    instances: list[packages.BundledPackage],
    io: cleo_io.IO,
    texts: dict[pathlib.Path, str],
) -> list[package_patches.PatchReroll]:
    rerolled: list[package_patches.PatchReroll] = []
    for candidate in candidates:
        candidate_version = Version(candidate.source_version)
        bases = [
            item
            for item in instances
            if Version(item.source_version) < candidate_version
            and Version(item.source_version).major == candidate_version.major
        ]
        if not bases:
            raise ValueError(
                f"cannot reroll {candidate.name} {candidate.source_version}: "
                "no known earlier release in the same major series"
            )
        patch_result = package_patches.reroll_patches(
            candidate,
            io,
            base_packages=tuple(
                sorted(bases, key=lambda item: Version(item.source_version))
            ),
        )
        for path, content in patch_result.writes.items():
            previous_content = texts.get(path)
            if previous_content is not None and previous_content != content:
                raise ValueError(f"conflicting generated patch: {path}")
            texts[path] = content
        rerolled.extend(patch_result.patches)
    return rerolled


def update_releases(
    *, check: bool = False, io: cleo_io.IO | None = None
) -> UpdateResult:
    grouped: dict[
        type[packages.BundledPackage], list[packages.BundledPackage]
    ] = {}
    for package in registered_releases():
        grouped.setdefault(type(package), []).append(package)

    changes: list[ReleaseChange] = []
    texts: dict[pathlib.Path, str] = {}
    rerolled: list[package_patches.PatchReroll] = []
    recipes = sorted(grouped, key=lambda item: (item.__module__, item.__name__))
    for recipe in recipes:
        instances = grouped[recipe]
        package = instances[-1]
        previous = tuple(
            sorted((item.source_version for item in instances), key=Version)
        )
        desired = _desired_releases(recipe, package)
        if desired == previous:
            continue
        changes.append(ReleaseChange(str(package.name), previous, desired))
        if check:
            continue
        if io is None:
            raise TypeError("io is required when applying updates")

        hashes, candidates = _candidate_releases(recipe, instances, desired, io)
        rewrite_declarations(recipe, hashes, texts)
        rerolled.extend(
            _collect_patch_updates(candidates, instances, io, texts)
        )

    if check and changes:
        detail = "; ".join(
            f"{item.package}: {', '.join(item.previous)} -> "
            f"{', '.join(item.current)}"
            for item in changes
        )
        raise ValueError(f"registered releases are outdated: {detail}")
    if not check:
        write_transaction(texts)
    return UpdateResult(releases=tuple(changes), patches=tuple(rerolled))
