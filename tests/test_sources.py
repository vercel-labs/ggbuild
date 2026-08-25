# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import Any, ClassVar, cast

import hashlib
import pathlib
import platform as host_platform
import tarfile
import tempfile
import unittest
from collections.abc import Sequence
from unittest import mock

import pytest
import requests
from lograil import (
    ProcessGroupResult,
    ProcessSpec,
    configure_logging,
    run_process_group,
)

from ggbuild import cache as package_cache, packages
from ggbuild.commands.build import Build
from ggbuild.packages import sources as package_sources
from ggbuild.targets.generic import platform
from ggbuild.targets.generic.build import Build as GenericBuild

from .registered_release_recipe import (
    RegisteredRelease as CommandRegisteredRelease,
)


class RegisteredRelease(packages.BundledPackage):
    title = "Registered release"
    ident = "registered-release"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/package-{version}.tar.gz"}
    ]


class MultipleSources(packages.BundledPackage):
    title = "Multiple sources"
    ident = "multiple-sources"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/first-{version}.tar.gz"},
        {"url": "https://example.com/second-{version}.tar.gz"},
    ]


class RegisteredReleaseTest(unittest.TestCase):
    def test_constructor_registers_verified_archive_and_aliases(self) -> None:
        digest = "a" * 64
        RegisteredRelease.aliases = ["registered-release-dev"]
        release = RegisteredRelease("1.2.3", sha256=digest)

        registered = RegisteredRelease.registered_release("1.2.3")
        self.assertIs(registered, release)
        self.assertEqual(release.sha256, digest)
        self.assertEqual(
            release.get_sources()[0].url,
            "https://example.com/package-1.2.3.tar.gz",
        )
        self.assertEqual(len(release.get_sources()[0].verifications), 1)
        self.assertIsNot(
            release.clone().get_sources()[0], release.get_sources()[0]
        )

    def test_sha256_rejects_multiple_sources(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one archive source"):
            MultipleSources("1.0", sha256="a" * 64)

    def test_build_root_reuses_registered_verified_release(self) -> None:
        command = Build()
        command._io = mock.Mock()  # ruff: ignore[private-member-access]
        options = mock.Mock(
            package=("tests.registered_release_recipe:RegisteredRelease"),
            version="1.2.3",
            revision=None,
            is_release=False,
            tags={},
        )

        root = command._root_package(  # ruff: ignore[private-member-access]
            options, platform.GenericTarget("test", "test")
        )

        registered = CommandRegisteredRelease.registered_release("1.2.3")
        self.assertIsNot(root, registered)
        self.assertEqual(root.sha256, "a" * 64)
        self.assertEqual(len(root.get_sources()[0].verifications), 1)


class DownloadCacheTest(unittest.TestCase):
    def test_cache_creation_tolerates_concurrent_creator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_dir = pathlib.Path(temporary) / "ggbuild"
            distfiles = cache_dir / "distfiles"
            distfiles.mkdir(parents=True)
            source = package_sources.HttpsSource(
                "https://example.com/archive.tar.gz",
                "archive.tar.gz",
            )
            path_exists = pathlib.Path.exists

            def concurrent_create(path: pathlib.Path) -> bool:
                if path == distfiles:
                    return False
                return path_exists(path)

            with (
                mock.patch.object(package_cache, "CACHEDIR", cache_dir),
                mock.patch.object(pathlib.Path, "exists", concurrent_create),
                mock.patch.object(
                    source,
                    "_download",
                    return_value=distfiles / source.name,
                ) as download,
            ):
                result = source.download(mock.Mock())

            self.assertEqual(result, distfiles / source.name)
            download.assert_called_once()


def _response(
    status: int,
    content: bytes = b"",
    *,
    headers: dict[str, str] | None = None,
) -> mock.Mock:
    response = mock.Mock(spec=requests.Response)
    response.status_code = status
    response.headers = headers or {}
    response.text = content.decode()
    response.iter_content.return_value = iter((content,))
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(str(status))
    return response


def test_download_retries_transient_http_errors() -> None:
    unavailable = _response(503)
    success = _response(200, b"archive")

    with (
        mock.patch(
            "ggbuild.packages.sources.requests.get",
            side_effect=(unavailable, success),
        ) as get,
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
        mock.patch(
            "ggbuild.packages.sources.time.monotonic_ns", return_value=0
        ),
    ):
        result = package_sources._request_with_retry(  # ruff: ignore[private-member-access]
            "https://example.com/archive", stream=True
        )

    assert result is success
    assert get.call_count == 2
    sleep.assert_called_once_with(1.0)
    unavailable.close.assert_called_once_with()


def test_download_retry_honors_bounded_retry_after() -> None:
    unavailable = _response(429, headers={"retry-after": "120"})
    success = _response(200, b"archive")

    with (
        mock.patch(
            "ggbuild.packages.sources.requests.get",
            side_effect=(unavailable, success),
        ),
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
    ):
        package_sources._request_with_retry(  # ruff: ignore[private-member-access]
            "https://example.com/archive", stream=True
        )

    sleep.assert_called_once_with(60.0)


def test_download_falls_back_to_checksum_verified_mirror(
    tmp_path: pathlib.Path,
) -> None:
    expected = b"verified archive"
    primary = _response(200, b"wrong archive")
    mirror = _response(200, expected)
    source = package_sources.HttpsSource(
        "https://primary.example/archive.tar.gz",
        "archive.tar.gz",
        mirrors=("https://mirror.example/archive.tar.gz",),
    )
    source.add_verification(
        package_sources.HashVerification(
            "sha256", hash_value=hashlib.sha256(expected).hexdigest()
        )
    )
    destination = tmp_path / source.name

    with mock.patch(
        "ggbuild.packages.sources.requests.get",
        side_effect=(primary, mirror),
    ) as get:
        result = source._download(destination, mock.Mock())  # ruff: ignore[private-member-access]

    assert result.read_bytes() == expected
    assert [call.args[0] for call in get.call_args_list] == list(source.urls)
    primary.close.assert_called_once_with()
    mirror.close.assert_called_once_with()


def test_download_uses_mirror_without_retrying_not_found(
    tmp_path: pathlib.Path,
) -> None:
    not_found = _response(404)
    mirror = _response(200, b"archive")
    source = package_sources.HttpsSource(
        "https://primary.example/archive.tar.gz",
        "archive.tar.gz",
        mirrors=("https://mirror.example/archive.tar.gz",),
    )

    with (
        mock.patch(
            "ggbuild.packages.sources.requests.get",
            side_effect=(not_found, mirror),
        ) as get,
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
    ):
        source._download(tmp_path / source.name, mock.Mock())  # ruff: ignore[private-member-access]

    assert get.call_count == 2
    sleep.assert_not_called()
    not_found.close.assert_called_once_with()


def test_download_removes_partial_file_when_all_locations_fail(
    tmp_path: pathlib.Path,
) -> None:
    partial = _response(200)
    partial.iter_content.side_effect = requests.ConnectionError("disconnected")
    not_found = _response(404)
    source = package_sources.HttpsSource(
        "https://primary.example/archive.tar.gz",
        "archive.tar.gz",
        mirrors=("https://mirror.example/archive.tar.gz",),
    )
    destination = tmp_path / source.name

    with (
        mock.patch(
            "ggbuild.packages.sources.requests.get",
            side_effect=(partial, not_found),
        ),
        pytest.raises(RuntimeError, match="all download locations failed"),
    ):
        source._download(destination, mock.Mock())  # ruff: ignore[private-member-access]

    assert not destination.exists()


class PrebuiltSourceTest(unittest.TestCase):
    def test_source_preparation_skips_prebuilt_packages(self) -> None:
        cached = mock.Mock(name="cached")
        cached.name = "cached"
        rebuilt = mock.Mock(name="rebuilt")
        rebuilt.name = "rebuilt"
        source = mock.Mock()
        rebuilt.get_sources.return_value = [source]
        build = mock.Mock()
        build.__dict__.update({
            "_bundled": [cached, rebuilt],
            "_prebuilt_packages": frozenset({"cached"}),
            "_tarballs": {},
            "_io": mock.Mock(),
            "_as_build": mock.Mock(),
        })
        build.get_tarball_root.return_value = pathlib.Path("/tarballs")
        build.get_tarball_tpl.return_value = "source{part}.tar{comp}"
        source.path = None
        tarball = pathlib.Path("/tarballs/source.tar")
        source.tarball.return_value = tarball

        GenericBuild.prepare_tarballs(build)

        cached.get_sources.assert_not_called()
        rebuilt.get_sources.assert_called_once_with()
        self.assertEqual(vars(build)["_tarballs"][rebuilt], [(source, tarball)])

    def test_patch_preparation_skips_prebuilt_packages(
        self,
    ) -> None:
        cached = mock.Mock(name="cached")
        cached.name = "cached"
        cached.get_patches.side_effect = AssertionError(
            "prebuilt package patches must not be prepared"
        )
        rebuilt = mock.Mock(name="rebuilt")
        rebuilt.name = "rebuilt"
        rebuilt.get_patches.return_value = {}
        build = mock.Mock()
        build.__dict__.update({
            "_bundled": [cached, rebuilt],
            "_prebuilt_packages": frozenset({"cached"}),
            "_patches": [],
        })
        with tempfile.TemporaryDirectory() as temporary:
            build.get_patches_root.return_value = pathlib.Path(temporary)
            GenericBuild.prepare_patches(build)

        rebuilt.get_patches.assert_called_once_with()


class LocalSourceTest(unittest.TestCase):
    def test_windows_file_uri_becomes_native_drive_path(self) -> None:
        with mock.patch.object(host_platform, "system", return_value="Windows"):
            source = package_sources.source_for_url(
                "file:///D:/a/ggbuild/tests/keepwork_source"
            )

        self.assertEqual(source.url, "D:/a/ggbuild/tests/keepwork_source")

    def test_windows_tarball_does_not_require_cli_transform_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source_dir = root / "source"
            source_dir.mkdir()
            (source_dir / "file.txt").write_text("content", encoding="utf-8")
            (source_dir / ".git").mkdir()
            (source_dir / ".git/config").write_text(
                "metadata", encoding="utf-8"
            )
            source = package_sources.LocalSource(
                str(source_dir), source_dir.name
            )

            with mock.patch.object(
                host_platform, "system", return_value="Windows"
            ):
                result = source.tarball(
                    mock.Mock(unique_name="package-1.0"),
                    target_dir=root,
                    io=mock.Mock(),
                    build=mock.Mock(),
                )

            with tarfile.open(result) as archive:
                names = archive.getnames()

        self.assertIn("package-1.0/file.txt", names)
        self.assertFalse(any(".git" in name for name in names))


class _TarBuild:
    def sh_get_command(self, command: str, *, relative_to: str) -> str:
        if command != "tar" or relative_to != "fsroot":
            raise AssertionError((command, relative_to))
        return "tar"


class ArchiveProgressTest(unittest.TestCase):
    def test_external_tar_extraction_streams_member_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = root / "source" / "package"
            (source / "subdirectory").mkdir(parents=True)
            (source / "first.txt").write_text("first", encoding="utf-8")
            (source / "subdirectory" / "second.txt").write_text(
                "second",
                encoding="utf-8",
            )
            archive = root / "source.tar.gz"
            with tarfile.open(archive, mode="w:gz") as archive_file:
                archive_file.add(source, arcname="package")

            destination = root / "destination"
            destination.mkdir()
            results: list[ProcessGroupResult] = []

            def record_process_group(
                specs: Sequence[ProcessSpec],
                **kwargs: Any,
            ) -> ProcessGroupResult:
                result = run_process_group(specs, **kwargs)
                results.append(result)
                return result

            with (
                mock.patch.dict("os.environ", {"LOGRAIL_OUTPUT": "plain"}),
                mock.patch.object(
                    package_sources,
                    "run_process_group",
                    side_effect=record_process_group,
                ),
            ):
                configure_logging()
                package_sources.unpack_tar(
                    archive,
                    destination,
                    build=cast("Any", _TarBuild()),
                    strip_components=1,
                )

            self.assertEqual(
                (destination / "first.txt").read_text(encoding="utf-8"),
                "first",
            )
            self.assertEqual(
                (destination / "subdirectory" / "second.txt").read_text(
                    encoding="utf-8"
                ),
                "second",
            )
            self.assertEqual(len(results), 1)
            process = results[0].processes[0]
            self.assertTrue(process.success)
            self.assertIn("v", process.spec.argv[1])
            progress_entries = [
                entry
                for entry in process.tail
                if "lograil.progress.completed" in entry
            ]
            self.assertEqual(len(progress_entries), 3)
            self.assertEqual(
                [
                    entry["lograil.progress.completed"]
                    for entry in progress_entries
                ],
                [1, 2, 3],
            )
            self.assertEqual(
                {entry["lograil.progress.total"] for entry in progress_entries},
                {3},
            )
            self.assertEqual(
                {
                    entry["lograil.progress.subject"]
                    for entry in progress_entries
                },
                {archive.name},
            )
            self.assertEqual(
                {
                    entry["lograil.progress.process"]
                    for entry in progress_entries
                },
                {"extracting"},
            )
            self.assertEqual(
                {
                    entry["lograil.progress.separator"]
                    for entry in progress_entries
                },
                {": "},
            )


if __name__ == "__main__":
    unittest.main()
