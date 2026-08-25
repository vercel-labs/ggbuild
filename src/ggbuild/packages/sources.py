# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    TypedDict,
)

import hashlib
import os
import pathlib
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Iterable

import requests
from lograil import ProcessSpec, progress, run_process_group, stage
from lograil.parsers import OutputParserCapabilities

from ggbuild import cache, tools

_DOWNLOAD_ATTEMPTS = 5
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(str(retry_after)), 60.0)
            except ValueError:
                pass
    backoff = float(min(2 ** (attempt - 1), 30))
    jitter = float(time.monotonic_ns() % 1000) / 1000
    return backoff + jitter


def _request_with_retry(url: str, *, stream: bool) -> requests.Response:
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        response: requests.Response | None = None
        try:
            response = requests.get(url, stream=stream, timeout=30)
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                try:
                    response.raise_for_status()
                except requests.HTTPError:
                    response.close()
                    raise
                return response
        except requests.ConnectionError, requests.Timeout:
            if attempt == _DOWNLOAD_ATTEMPTS:
                raise
        else:
            if attempt == _DOWNLOAD_ATTEMPTS:
                try:
                    response.raise_for_status()
                finally:
                    response.close()
            response.close()
        time.sleep(_retry_delay(response, attempt))
    raise AssertionError("unreachable")


if TYPE_CHECKING:
    from cleo.io import io as cleo_io

    from ggbuild import packages as mpkg, targets


class SourceDeclBase(TypedDict):
    url: str


class SourceDecl(SourceDeclBase, total=False):
    csum: str | None
    csum_url: str | None
    csum_algo: str | None
    extras: SourceExtraDecl | None
    mirrors: list[str]


class SourceExtraDecl(TypedDict, total=False):
    exclude_submodules: list[str]
    clone_depth: int
    version: str
    vcs_version: str
    include_gitdir: bool
    archive: bool
    mirrors: list[str]


class BaseVerification:
    def verify(self, path: pathlib.Path) -> None:
        raise NotImplementedError


class HashVerification(BaseVerification):
    def __init__(
        self,
        algorithm: str,
        *,
        hash_url: str | None = None,
        hash_value: str | None = None,
    ) -> None:
        self.algorithm = algorithm
        self._hash_value: str | None
        if hash_value is not None:
            self._hash_value = hash_value
        elif hash_url is not None:
            self._hash_url = hash_url
            self._hash_value = None
        else:
            raise ValueError(
                "either hash_url or hash_value is required for HashVerification"
            )

    def verify(self, path: pathlib.Path) -> None:
        if self._hash_value is None:
            self._obtain_hash_value()

        hashfunc = hashlib.new(self.algorithm)
        with (
            stage(
                f"verify/{self.algorithm}",
                process="verify",
                subject=path.name,
            ),
            progress(
                process="verify",
                subject=path.name,
                description=f"reading {path.name}",
                total=path.stat().st_size,
            ) as verification_progress,
            pathlib.Path(path).open("rb") as f,
        ):
            while chunk := f.read(1024 * 1024):
                hashfunc.update(chunk)
                verification_progress.advance(len(chunk))

        if hashfunc.hexdigest() != self._hash_value:
            raise ValueError(
                f"{path} does not match expected {self.algorithm} value of "
                f"{self._hash_value}"
            )

    def _obtain_hash_value(self) -> str:
        response = _request_with_retry(str(self._hash_url), stream=False)
        try:
            content = response.text.strip()
        finally:
            response.close()
        firstval, _, _rest = content.partition(" ")
        self._hash_value = firstval
        return firstval


class BaseSource:
    def __init__(
        self,
        url: str,
        name: str,
        path: str | None = None,
        **extras: Any,
    ) -> None:
        self.url = url
        self.verifications: list[BaseVerification] = []
        self.name = name
        self.extras = extras
        self.path = path

    def add_verification(self, verification: BaseVerification) -> None:
        self.verifications.append(verification)

    def verify(self, path: pathlib.Path) -> None:
        for verification in self.verifications:
            verification.verify(path)

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        raise NotImplementedError

    def tarball(
        self,
        pkg: mpkg.BasePackage,
        name_tpl: str | None = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        raise NotImplementedError


class HttpsSource(BaseSource):
    def __init__(
        self,
        url: str,
        name: str,
        path: str | None = None,
        *,
        mirrors: Iterable[str] = (),
        **extras: Any,
    ) -> None:
        super().__init__(url, name, path, **extras)
        self.urls = tuple(dict.fromkeys((url, *mirrors)))

    def download(self, io: cleo_io.IO) -> pathlib.Path:
        destination_dir = cache.cachedir() / "distfiles"
        destination_dir.mkdir(exist_ok=True)

        destination = destination_dir / self.name
        if destination.exists():
            try:
                self.verify(destination)
            except OSError, ValueError:
                io.write_line(
                    f"<warning>Cached {self.name} exists, but does not pass "
                    f"verification.  Downloading anew."
                )
            else:
                return destination

        return self._download(destination, io)

    def _download(
        self, destination: pathlib.Path, io: cleo_io.IO
    ) -> pathlib.Path:
        failures: list[str] = []
        for url in self.urls:
            try:
                return self._download_url(url, destination, io)
            except (OSError, ValueError, requests.RequestException) as error:
                destination.unlink(missing_ok=True)
                failures.append(f"{url}: {error}")
        raise RuntimeError(
            f"all download locations failed for {self.name}: "
            + "; ".join(failures)
        )

    def _download_url(
        self, url: str, destination: pathlib.Path, io: cleo_io.IO
    ) -> pathlib.Path:
        req = _request_with_retry(url, stream=True)
        try:
            length_header = req.headers.get("content-length")
            length = int(length_header) if length_header is not None else None
            with (
                stage(
                    "source/download",
                    process="download",
                    subject=self.name,
                ),
                progress(
                    process="download",
                    subject=self.name,
                    description=url,
                    total=length,
                ) as download_progress,
                pathlib.Path(destination).open("wb") as f,
            ):
                for chunk in req.iter_content(chunk_size=4096):
                    if chunk:
                        download_progress.advance(len(chunk))
                        f.write(chunk)
        except OSError, ValueError, requests.RequestException:
            if destination.exists():
                destination.unlink()
            raise
        finally:
            req.close()
        try:
            self.verify(destination)
        except OSError, ValueError:
            destination.unlink()
            raise

        return destination

    def _tarball(
        self,
        pkg: mpkg.BasePackage | None = None,
        name_tpl: str | None = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        part: str = "",
        build: targets.Build | None = None,
    ) -> pathlib.Path:
        if name_tpl is None:
            if pkg is None:
                raise ValueError("pkg is required when name_tpl is omitted")
            name_tpl = f"{pkg.unique_name}{{part}}.tar{{comp}}"
        src = self.download(io)
        copy = True
        target_path = None
        if "archive" in self.extras and not self.extras["archive"]:
            copy = False
            comp = ".gz"
            target_path = target_dir / name_tpl.format(part=part, comp=comp)
            with tarfile.open(target_path, "w:gz") as tf:
                tf.add(
                    str(src),
                    arcname=str(pathlib.Path(src.name) / src.name),
                )
        elif src.suffix == ".tgz":
            comp = ".gz"
        elif src.suffix == ".tbz2":
            comp = ".bzip2"
        elif src.suffix != ".tar" and ".tar" in src.suffixes:
            comp = src.suffix
        elif src.suffix == ".zip":
            comp = ".gz"
            target_path = target_dir / name_tpl.format(part=part, comp=comp)
            with tempfile.TemporaryDirectory() as tmpdir:
                destdir = pathlib.Path(tmpdir)
                unpack(
                    src, dest=destdir, io=io, strip_components=0, build=build
                )
                subdirs = list(destdir.iterdir())
                if len(subdirs) > 1:
                    raise RuntimeError(
                        "multiple top-level directories in source archive"
                    )
                subdir = subdirs[0]
                with tarfile.open(target_path, "w:gz") as tf:
                    tf.add(subdir, arcname=subdir.name)
            copy = False
        else:
            raise RuntimeError(f"unsupported archive format: {src.suffix}")

        if copy:
            target_path = target_dir / name_tpl.format(part=part, comp=comp)
            shutil.copy(src, target_path)

        if target_path is None:
            raise RuntimeError("source archive target was not selected")
        return target_path

    def tarball(
        self,
        pkg: mpkg.BasePackage | None = None,
        name_tpl: str | None = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        return self._tarball(
            pkg, name_tpl, part=part, build=build, target_dir=target_dir, io=io
        )

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        self.download(io)
        with tempfile.TemporaryDirectory() as t:
            tardir = pathlib.Path(t)
            tarball = self._tarball(
                name_tpl="tmp{part}.tar{comp}",
                target_dir=tardir,
                io=io,
            )
            unpack(tarball, dest=target_dir, io=io)


class LocalSource(BaseSource):
    def tarball(
        self,
        pkg: mpkg.BasePackage,
        name_tpl: str | None = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        comp = ".gz"
        if name_tpl is None:
            name_tpl = f"{pkg.unique_name}{{part}}.tar{{comp}}"
        target_path = target_dir / name_tpl.format(part=part, comp=comp)

        if platform.system() == "Windows":
            with tarfile.open(target_path, "w:gz") as archive:
                archive.add(
                    self.url,
                    arcname=pkg.unique_name,
                    filter=_exclude_vcs_member,
                )
            return target_path

        tar = _split_command(build.sh_get_command("tar"))
        tools.cmd(
            *tar,
            *[
                f"--directory={self.url}",
                "--exclude-vcs",
                "--exclude-vcs-ignores",
                "--create",
                "--gzip",
                f"--transform=flags=r;s|^\\./|{pkg.unique_name}/|",
                f"--file={target_path}",
                ".",
            ],
        )

        return target_path

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        shutil.copytree(self.url, target_dir)


class GitSource(BaseSource):
    def __init__(
        self,
        url: str,
        name: str,
        *,
        vcs_version: str | None = None,
        exclude_submodules: Iterable[str] | None = None,
        clone_depth: int | None = None,
        include_gitdir: bool = False,
        force_archive: Iterable[str] | None = None,
    ) -> None:
        super().__init__(url, name)
        self.ref = vcs_version
        if exclude_submodules is not None:
            self.exclude_submodules = frozenset(exclude_submodules)
        else:
            self.exclude_submodules = frozenset()
        if force_archive is not None:
            self.force_archive = frozenset(force_archive)
        else:
            self.force_archive = frozenset()
        self.clone_depth = clone_depth
        self.include_gitdir = include_gitdir
        self._clone: tools.git.GitClone | None = None

    def download(self, io: cleo_io.IO) -> tools.git.GitClone:
        if self._clone is None:
            self._clone = tools.git.clone_repo(
                self.url,
                remote_ref=self.ref,
                clean_checkout=os.environ.get("METAPKG_GIT_CACHE")
                == "disabled",
                exclude_submodules=self.exclude_submodules,
                clone_depth=self.clone_depth,
            )
        return self._clone

    def copy(
        self,
        target_dir: pathlib.Path,
        *,
        io: cleo_io.IO,
    ) -> None:
        repo = self.download(io)
        repo.run(
            "checkout-index",
            "-a",
            "-f",
            f"--prefix={target_dir}",
        )

    def tarball(
        self,
        pkg: mpkg.BasePackage,
        name_tpl: str | None = None,
        *,
        target_dir: pathlib.Path,
        io: cleo_io.IO,
        build: targets.Build,
        part: str = "",
    ) -> pathlib.Path:
        repo = self.download(io)
        if name_tpl is None:
            name_tpl = f"{pkg.unique_name}{{part}}.tar{{comp}}"
        target_path = target_dir / name_tpl.format(part=part, comp="")

        repo.run(
            "archive",
            f"--output={target_path}",
            "--format=tar",
            f"--prefix={pkg.unique_name}/",
            "HEAD",
        )

        submodules = repo.run(
            "submodule", "foreach", "--quiet", "--recursive", "echo $sm_path"
        )

        for submodule_line in submodules.splitlines():
            submodule_path = submodule_line.strip()
            if not submodule_path:
                continue
            io.write_line(f"<info>Archiving git submodule in {submodule_path}")
            module_repo = tools.git.Git(repo.work_tree / submodule_path)
            with tempfile.TemporaryDirectory() as temporary:
                archive_file = pathlib.Path(temporary) / "submodule.tar"
                module_repo.run(
                    "archive",
                    "--format=tar",
                    f"--output={archive_file}",
                    f"--prefix={pkg.unique_name}/{submodule_path}/",
                    "HEAD",
                )
                self._tar_append(archive_file, target_path)

        if self.include_gitdir:
            prefix = f"{pkg.unique_name}/.git/"
            with tarfile.open(target_path, "a") as tf:
                tf.add(repo.git_dir, prefix)

        if self.force_archive:
            with tarfile.open(target_path, "a") as tf:
                for path in self.force_archive:
                    repo_path = repo.work_dir / path
                    prefix = f"{pkg.unique_name}/{path}"
                    if prefix not in set(tf.getnames()):
                        tf.add(repo_path, prefix)

        tools.cmd("gzip", target_path, cwd=target_dir)
        return pathlib.Path(f"{target_path}.gz")

    def _tar_append(
        self,
        source_tarball: pathlib.Path,
        target_tarball: pathlib.Path,
    ) -> None:
        if platform.system() != "Linux":
            with (
                tarfile.open(source_tarball) as modf,
                tarfile.open(target_tarball, "a") as tf,
            ):
                for m in modf.getmembers():
                    if m.issym():
                        # Skip broken symlinks.
                        link_parent = pathlib.Path(m.name).parent.as_posix()
                        target = os.path.normpath(
                            "/".join(filter(None, (link_parent, m.linkname)))
                        )
                        try:
                            modf.getmember(target)
                        except KeyError:
                            continue
                    tf.addfile(m, modf.extractfile(m))

        else:
            tools.cmd(
                "tar",
                "--concatenate",
                "--file",
                target_tarball,
                source_tarball,
            )


_VCS_DIRECTORY_NAMES = frozenset({
    ".bzr",
    ".git",
    ".hg",
    ".svn",
    "CVS",
    "RCS",
    "SCCS",
    "_darcs",
})


def _exclude_vcs_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = pathlib.PurePosixPath(member.name).parts
    return None if _VCS_DIRECTORY_NAMES.intersection(parts) else member


def source_for_url(
    url: str,
    extras: SourceExtraDecl | None = None,
) -> BaseSource:
    parts = urllib.parse.urlparse(url)
    path_parts = parts.path.split("/")
    name = path_parts[-1]
    if extras is None:
        extras = {}
    if parts.scheme in {"https", "http"}:
        return HttpsSource(url, name=name, **extras)
    if parts.scheme.startswith("git+"):
        vcs_version = extras.get("vcs_version") or extras.get("version")
        return GitSource(
            url[4:],
            name=name,
            vcs_version=vcs_version,
            exclude_submodules=extras.get("exclude_submodules"),
            clone_depth=extras.get("clone_depth"),
            include_gitdir=extras.get("include_gitdir", False),
        )
    if parts.scheme == "file":
        path = urllib.parse.unquote(parts.path)
        if parts.netloc:
            path = f"//{parts.netloc}{path}"
        elif platform.system() == "Windows" and re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return LocalSource(path, name, **extras)
    raise ValueError(f"unsupported source URL scheme: {parts.scheme}")


def unpack(
    archive: pathlib.Path,
    dest: pathlib.Path,
    io: cleo_io.IO,
    *,
    build: targets.Build | None = None,
    strip_components: int = 1,
) -> None:
    parts = archive.name.split(".")
    if len(parts) == 1:
        raise ValueError(f"{archive.name} is not a supported archive")

    if not dest.exists():
        dest.mkdir()

    ext = parts[-1]

    if parts[-2] == "tar" or ext in {"tgz", "tbz2", "tar"}:
        with stage(
            "source/extract",
            process="extracting",
            subject=archive.name,
        ):
            unpack_tar(
                archive, dest, build=build, strip_components=strip_components
            )
    elif parts[-1] == "zip":
        with stage(
            "source/extract",
            process="extracting",
            subject=archive.name,
        ):
            unpack_zip(
                archive, dest, build=build, strip_components=strip_components
            )
    else:
        raise ValueError(f"{archive.name} is not a supported archive")


def _split_command(command: str) -> list[str]:
    return shlex.split(command, posix=platform.system() != "Windows")


def _tar_compression(ext: str, *, command_line: bool) -> str:
    compressions = {
        ".gz": "z" if command_line else "gz",
        ".tgz": "z" if command_line else "gz",
        ".bz2": "j" if command_line else "bz2",
        ".tbz2": "j" if command_line else "bz2",
        ".xz": "J" if command_line else "xz",
    }
    try:
        return compressions[ext]
    except KeyError:
        raise ValueError(f"{ext} is not a supported archive suffix") from None


def _strip_tar_member(
    member: tarfile.TarInfo,
    strip_components: int,
) -> tarfile.TarInfo | None:
    if not strip_components:
        return member
    member_parts = pathlib.Path(member.name).parts
    if len(member_parts) <= strip_components:
        return None
    member.name = str(pathlib.Path(*member_parts[strip_components:]))
    return member


class _TarExtractionParser:
    capabilities = OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    )

    def __init__(self, *, subject: str, total: int) -> None:
        self._subject = subject
        self._total = total
        self._completed = 0

    def __call__(self, entry: dict[str, Any]) -> dict[str, Any]:
        message = str(entry.get("message", ""))
        if message and self._completed < self._total:
            self._completed += 1
        entry.update({
            "lograil.status.detail": message,
            "lograil.progress.description": message,
            "lograil.progress.completed": self._completed,
            "lograil.progress.total": self._total,
            "lograil.progress.process": "extracting",
            "lograil.progress.subject": self._subject,
            "lograil.progress.separator": ": ",
        })
        return entry


def _count_tar_members(
    archive: pathlib.PurePath,
    *,
    strip_components: int,
) -> int:
    count = 0
    with (
        progress(
            process="index",
            subject=archive.name,
            description="indexing archive",
        ),
        tarfile.open(archive, mode="r:*") as archive_file,
    ):
        for member in archive_file:
            if len(pathlib.PurePosixPath(member.name).parts) > strip_components:
                count += 1
    return count


def unpack_tar(
    archive: pathlib.PurePath,
    dest: pathlib.PurePath,
    *,
    build: targets.Build | None = None,
    strip_components: int,
) -> None:
    ext = archive.suffix

    if build is not None:
        compression = _tar_compression(ext, command_line=True)
        member_count = _count_tar_members(
            archive,
            strip_components=strip_components,
        )

        tar_command = _split_command(
            build.sh_get_command("tar", relative_to="fsroot")
        )
        args = [
            f"-xv{compression}",
            f"-f{archive}",
            f"-C{dest}",
        ]
        if strip_components:
            args.append(f"--strip-components={strip_components}")

        command = [*tar_command, *args]
        result = run_process_group([
            ProcessSpec(
                command,
                process="extracting",
                subject=archive.name,
                category="sources",
                stream="combined",
                parser=_TarExtractionParser(
                    subject=archive.name,
                    total=member_count,
                ),
            )
        ])
        process_result = result.processes[0]
        if not process_result.success:
            raise subprocess.CalledProcessError(
                process_result.exit_code,
                command,
            )
        return

    _tar_compression(ext, command_line=False)
    with tarfile.open(archive, mode="r:*") as archive_file:
        members = archive_file.getmembers()
        with progress(
            process="extracting",
            subject=archive.name,
            description="archive members",
            total=len(members),
            separator=": ",
        ) as extraction_progress:
            for member in members:
                stripped_member = _strip_tar_member(member, strip_components)
                if stripped_member is not None:
                    archive_file.extract(stripped_member, path=dest)
                extraction_progress.advance()


def unpack_zip(
    archive: pathlib.Path,
    dest: pathlib.Path,
    *,
    strip_components: int,
    build: targets.Build | None = None,
) -> None:
    zf = zipfile.ZipFile(archive)

    try:
        members = zf.infolist()
        with progress(
            process="extracting",
            subject=archive.name,
            description="archive members",
            total=len(members),
            separator=": ",
        ) as extraction_progress:
            for member in members:
                _extract_zip_member(
                    zf,
                    member,
                    dest=dest,
                    strip_components=strip_components,
                )
                extraction_progress.advance()
    finally:
        zf.close()


def _extract_zip_member(
    zf: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    dest: pathlib.Path,
    strip_components: int,
) -> None:
    if strip_components:
        member_parts = pathlib.Path(member.filename).parts
        if len(member_parts) == strip_components:
            return

        relpath = pathlib.Path(member_parts[strip_components]).joinpath(
            *member_parts[strip_components + 1 :]
        )
    else:
        relpath = pathlib.Path(member.filename)
    targetpath = dest / relpath
    if member.is_dir():
        targetpath.mkdir(parents=True, exist_ok=True)
    else:
        dirname = targetpath.parent
        if not dirname.exists():
            dirname.mkdir(parents=True)
        with pathlib.Path(targetpath).open("wb") as df, zf.open(member) as sf:
            shutil.copyfileobj(sf, df)
