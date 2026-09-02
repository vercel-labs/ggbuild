# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Version-aware recipe patch selection and rerolling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import dataclasses
import email.parser
import email.policy
import email.utils
import inspect
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

import packaging.version

from ggbuild import tools

if TYPE_CHECKING:
    from cleo.io import io as cleo_io

    from ggbuild.packages import BundledPackage


_VERSION = r"[0-9]+(?:\.[0-9]+)*(?:[A-Za-z][A-Za-z0-9.]*)?(?:\+[A-Za-z0-9.]+)?"
_PATCH_FILE_RE = re.compile(
    rf"^(?P<pkg>[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)"
    rf"__(?P<name>[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)"
    rf"(?:__(?:(?P<minimum>{_VERSION})"
    rf"(?:(?P<range>-)(?P<maximum>{_VERSION})?)?"
    rf"|-(?P<open_maximum>{_VERSION})))?$",
    re.IGNORECASE,
)
_LEGACY_VERSION = re.compile(rf"(?P<name>.+)-(?P<version>{_VERSION})")
_PATCH_SUBJECT = re.compile(r"^\[PATCH[^]]*\]\s*", re.IGNORECASE)
_ACCEPTABLE_FUZZ = 2


class PatchError(RuntimeError):
    """A recipe patch could not be selected or rerolled."""


@dataclasses.dataclass(frozen=True, slots=True)
class PatchVariant:
    package: str
    name: str
    path: pathlib.Path
    minimum: packaging.version.Version | None = None
    maximum: packaging.version.Version | None = None
    exact: packaging.version.Version | None = None

    def matches(self, version: str) -> bool:
        candidate = packaging.version.Version(version)
        if self.exact is not None:
            return candidate == self.exact
        if self.minimum is not None and candidate < self.minimum:
            return False
        return self.maximum is None or candidate < self.maximum

    def narrower_than(self, other: PatchVariant) -> bool:
        """Return whether this variant is a strict subset of *other*."""
        if self.exact is not None:
            return other.exact is None or self.exact == other.exact
        if other.exact is not None:
            return False
        lower_inside = other.minimum is None or (
            self.minimum is not None and self.minimum >= other.minimum
        )
        upper_inside = other.maximum is None or (
            self.maximum is not None and self.maximum <= other.maximum
        )
        if not lower_inside or not upper_inside:
            return False
        return self.minimum != other.minimum or self.maximum != other.maximum

    @property
    def content(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclasses.dataclass(frozen=True, slots=True)
class PatchReroll:
    package: str
    name: str
    source: pathlib.Path
    destination: pathlib.Path | None

    @property
    def retained(self) -> bool:
        return self.destination is None


@dataclasses.dataclass(frozen=True, slots=True)
class RerollResult:
    patches: tuple[PatchReroll, ...]
    writes: dict[pathlib.Path, str]


def recipe_directory(recipe: type[BundledPackage]) -> pathlib.Path:
    source = inspect.getsourcefile(recipe)
    if source is None:
        raise PatchError(f"recipe source not found for {recipe!r}")
    return pathlib.Path(source).parent


def parse_patch(path: pathlib.Path) -> PatchVariant:
    match = _PATCH_FILE_RE.fullmatch(path.stem)
    if match is None:
        raise PatchError(f"malformed patch name: {path.name}")
    package = match.group("pkg")
    name = match.group("name")
    minimum_text = match.group("minimum")
    maximum_text = match.group("maximum") or match.group("open_maximum")
    exact: packaging.version.Version | None = None

    if minimum_text is not None and match.group("range") is None:
        exact = packaging.version.Version(minimum_text)
    elif minimum_text is None and maximum_text is None:
        legacy = _LEGACY_VERSION.fullmatch(name)
        if legacy is not None:
            name = legacy.group("name")
            legacy_version = packaging.version.Version(legacy.group("version"))
            if len(legacy_version.release) == 1:
                minimum_text = str(legacy_version)
                maximum_text = str(legacy_version.major + 1)
            else:
                exact = legacy_version

    return PatchVariant(
        package=package,
        name=name,
        path=path,
        minimum=(
            packaging.version.Version(minimum_text)
            if minimum_text is not None and exact is None
            else None
        ),
        maximum=(
            packaging.version.Version(maximum_text)
            if maximum_text is not None
            else None
        ),
        exact=exact,
    )


def patch_variants(recipe: type[BundledPackage]) -> tuple[PatchVariant, ...]:
    directory = recipe_directory(recipe) / "patches"
    if not directory.is_dir():
        return ()
    paths = sorted(directory.glob("*.patch"))
    return tuple(parse_patch(path) for path in paths)


# @lat: [[maintenance#Release Maintenance#Version-Aware Patch Selection]]
def select_patch_variants(package: BundledPackage) -> tuple[PatchVariant, ...]:
    grouped: dict[tuple[str, str], list[PatchVariant]] = {}
    for variant in patch_variants(type(package)):
        grouped.setdefault((variant.package, variant.name), []).append(variant)

    selected: list[PatchVariant] = []
    for key in sorted(grouped):
        matches = [
            variant
            for variant in grouped[key]
            if variant.matches(package.source_version)
        ]
        if not matches:
            continue
        narrowest = [
            candidate
            for candidate in matches
            if not any(
                other.narrower_than(candidate)
                for other in matches
                if other is not candidate
            )
        ]
        if len(narrowest) != 1:
            names = ", ".join(item.path.name for item in narrowest)
            raise PatchError(
                f"ambiguous patches for {key[0]}:{key[1]} at "
                f"{package.source_version}: {names}"
            )
        selected.append(narrowest[0])
    return tuple(selected)


def patches_for_build(
    package: BundledPackage,
) -> dict[str, list[tuple[str, str]]]:
    result: dict[str, list[tuple[str, str]]] = {}
    for patch in select_patch_variants(package):
        result.setdefault(patch.package, []).append((patch.name, patch.content))
    return result


def _initialize_repository(worktree: pathlib.Path) -> tools.git.Git:
    repo = tools.git.Git.initialize(worktree)
    repo.run("config", "user.name", "ggbuild")
    repo.run("config", "user.email", "ggbuild@localhost")
    repo.run("config", "commit.gpgsign", "false")
    repo.run("add", "--all")
    environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    repo.run("commit", "--quiet", "-m", "Pristine source", env=environment)
    return repo


def _materialize_source(
    package: BundledPackage,
    worktree: pathlib.Path,
    io: cleo_io.IO,
) -> None:
    from ggbuild.packages import (  # ruff: ignore[import-outside-top-level]
        sources as package_sources,
    )

    sources = package.get_sources()
    if len(sources) != 1:
        raise PatchError(
            f"{package.name} patch rerolling requires one primary source"
        )
    source = sources[0]
    if isinstance(source, package_sources.HttpsSource):
        archive = source.download(io)
        package_sources.unpack(
            archive,
            worktree,
            io,
            strip_components=source.extras.get("strip_components", 1),
        )
    elif isinstance(
        source, (package_sources.GitSource, package_sources.LocalSource)
    ):
        if isinstance(source, package_sources.LocalSource):
            shutil.copytree(source.url, worktree, dirs_exist_ok=True)
        else:
            source.copy(worktree, io=io)
    else:
        source_type = type(source).__name__
        raise PatchError(
            f"unsupported source type for patch rerolling: {source_type}"
        )


def _format_patch(repo: tools.git.Git) -> str:
    return repo.run(
        "format-patch",
        "--stdout",
        "--stat",
        "-1",
        "HEAD",
        strip_output=False,
    )


def _patch_applies(
    worktree: pathlib.Path,
    patch: pathlib.Path,
    *,
    reverse: bool = False,
    dry_run: bool = True,
) -> bool:
    args = [
        "patch",
        "-f",
        "-s",
        "-V",
        "none",
        "-r",
        "-",
        "-F",
        str(_ACCEPTABLE_FUZZ),
        "-p1",
    ]
    if dry_run:
        args.append("--dry-run")
    if reverse:
        args.append("-R")
    args.extend(["-i", str(patch)])
    try:
        tools.cmd(
            *args,
            cwd=worktree,
            hide_stderr=True,
            errors_are_fatal=False,
        )
    except subprocess.CalledProcessError:
        return False
    return True


def _commit_applied_patch(repo: tools.git.Git, patch: PatchVariant) -> None:
    message = email.parser.Parser(policy=email.policy.default).parsestr(
        patch.content
    )
    subject = _PATCH_SUBJECT.sub("", str(message.get("Subject", patch.name)))
    commit_message = subject
    author_name, author_email = email.utils.parseaddr(
        str(message.get("From", ""))
    )
    author = (
        f"{author_name} <{author_email}>"
        if author_name and author_email
        else "ggbuild <ggbuild@localhost>"
    )
    args = ["commit", "--quiet", "--no-verify", "--author", author]
    if date := message.get("Date"):
        args.extend(["--date", str(date)])
    args.extend(["-m", commit_message])
    repo.run("add", "--all")
    repo.run(*args)


def _conflict_detail(repo: tools.git.Git) -> str:
    status = repo.run_or("status", "--short", default="") or ""
    current = repo.run_or("am", "--show-current-patch=diff", default="") or ""
    parts = []
    if status:
        parts.append(f"status:\n{status}")
    if current:
        parts.append(f"failed patch:\n{current}")
    return "\n\n".join(parts) or "no Git diagnostics available"


def _reroll_patch(
    package: BundledPackage,
    patch: PatchVariant,
    application_patch: pathlib.Path | None,
    repo: tools.git.Git,
    worktree: pathlib.Path,
) -> tuple[PatchReroll, str | None]:
    failed = "__ggbuild_git_failed__"
    reverse = repo.run_or(
        "apply",
        "--check",
        "--reverse",
        patch.path,
        default=failed,
        hide_stderr=True,
    )
    if reverse != failed:
        raise PatchError(
            f"{patch.path.name} is already present in {package.name} "
            f"{package.source_version}; adjust its version constraints "
            "manually"
        )
    clean = _patch_applies(worktree, patch.path)
    if clean:
        if not _patch_applies(worktree, patch.path, dry_run=False):
            raise PatchError(
                f"could not apply {patch.path.name} after checking it"
            )
        _commit_applied_patch(repo, patch)
        return (
            PatchReroll(
                package=patch.package,
                name=patch.name,
                source=patch.path,
                destination=None,
            ),
            None,
        )
    if application_patch is None:
        raise PatchError(
            f"could not reconstruct {patch.path.name} from a known version"
        )
    applied = repo.run_or(
        "-c",
        "commit.gpgsign=false",
        "am",
        "--3way",
        "--no-verify",
        "--committer-date-is-author-date",
        application_patch,
        default=failed,
        input="",
    )
    if applied == failed:
        detail = _conflict_detail(repo)
        raise PatchError(
            f"could not reroll {patch.path.name} for "
            f"{package.source_version}. The worktree was preserved at "
            f"{worktree}. Resolve the conflict there, then inspect "
            f"`git format-patch --stdout -1 HEAD`.\n\n{detail}"
        )
    destination = patch.path.parent / (
        f"{patch.package}__{patch.name}__{package.source_version}.patch"
    )
    return (
        PatchReroll(
            package=patch.package,
            name=patch.name,
            source=patch.path,
            destination=destination,
        ),
        _format_patch(repo),
    )


def _reroll_series(
    package: BundledPackage,
    selected: tuple[PatchVariant, ...],
    base_packages: tuple[BundledPackage, ...],
    repo: tools.git.Git,
    root: pathlib.Path,
    worktree: pathlib.Path,
    io: cleo_io.IO,
) -> RerollResult:
    rerolled: list[PatchReroll] = []
    writes: dict[pathlib.Path, str] = {}
    for patch in selected:
        application_patch = None
        if not _patch_applies(worktree, patch.path) and base_packages:
            history_root = root / "history" / patch.path.stem
            history_root.mkdir(parents=True)
            base_repos, application_patches = _historical_patch_commits(
                base_packages, (patch,), history_root, io
            )
            for base_repo in base_repos:
                repo.run(
                    "fetch", "--quiet", "--no-tags", base_repo.work_dir, "HEAD"
                )
            application_patch = application_patches[patch.path]
        result, content = _reroll_patch(
            package,
            patch,
            application_patch,
            repo,
            worktree,
        )
        rerolled.append(result)
        if result.destination is not None and content is not None:
            writes[result.destination] = content
    return RerollResult(patches=tuple(rerolled), writes=writes)


def _historical_patch_commits(
    packages: tuple[BundledPackage, ...],
    selected: tuple[PatchVariant, ...],
    root: pathlib.Path,
    io: cleo_io.IO,
) -> tuple[tuple[tools.git.Git, ...], dict[pathlib.Path, pathlib.Path]]:
    """Recreate patch commits against an instantiated archive release."""
    generated = root / "patches"
    generated.mkdir()
    application_patches: dict[pathlib.Path, pathlib.Path] = {}
    unresolved = list(selected)
    repositories: list[tools.git.Git] = []
    for base_index, package in enumerate(packages):
        worktree = root / f"base-{base_index}"
        worktree.mkdir()
        _materialize_source(package, worktree, io)
        applicable = [
            patch
            for patch in unresolved
            if _patch_applies(worktree, patch.path)
        ]
        if not applicable:
            continue
        repo = _initialize_repository(worktree)
        repositories.append(repo)
        for patch in applicable:
            if not _patch_applies(worktree, patch.path, dry_run=False):
                raise PatchError(
                    f"could not apply {patch.path.name} to known base "
                    f"{package.source_version}"
                )
            _commit_applied_patch(repo, patch)
            generated_patch = generated / (
                f"{len(application_patches):04d}-{patch.path.name}"
            )
            generated_patch.write_text(_format_patch(repo), encoding="utf-8")
            application_patches[patch.path] = generated_patch
            unresolved.remove(patch)
        if not unresolved:
            break
    if unresolved:
        names = ", ".join(patch.path.name for patch in unresolved)
        versions = ", ".join(package.source_version for package in packages)
        raise PatchError(
            f"could not reconstruct patches from known versions "
            f"{versions}: {names}"
        )
    return tuple(repositories), application_patches


# @lat: [[maintenance#Release Maintenance#Patch Rerolling]]
def reroll_patches(
    package: BundledPackage,
    io: cleo_io.IO,
    *,
    base_packages: tuple[BundledPackage, ...] = (),
) -> RerollResult:
    selected = select_patch_variants(package)
    if not selected:
        return RerollResult(patches=(), writes={})
    foreign = sorted({
        item.package for item in selected if item.package != package.name
    })
    if foreign:
        raise PatchError(
            f"{package.name} patches target other source trees: "
            + ", ".join(foreign)
        )
    repository_url = type(package).canonical_repo
    if not repository_url:
        raise PatchError(
            f"{type(package).__name__} must declare canonical_repo to "
            "reroll patches"
        )

    prefix = f"ggbuild-reroll-{package.name}-"
    root = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    worktree = root / "worktree"
    worktree.mkdir()
    try:
        _materialize_source(package, worktree, io)
        repo = _initialize_repository(worktree)
        canonical_ref = type(package).canonical_ref(package.source_version)
        canonical = tools.git.clone_repo(
            repository_url, remote_ref=canonical_ref
        )
        if not base_packages:
            # The shared clone is partial, so fetch directly when canonical
            # history itself must provide the patch's old blobs.
            repo.run(
                "fetch",
                "--quiet",
                "--no-tags",
                repository_url,
                canonical.head,
            )

        result = _reroll_series(
            package, selected, base_packages, repo, root, worktree, io
        )
    except PatchError as error:
        if "worktree was preserved" not in str(error):
            shutil.rmtree(root, ignore_errors=True)
        raise
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    else:
        shutil.rmtree(root, ignore_errors=True)
        return result
