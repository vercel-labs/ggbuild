# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import Any, ClassVar, cast

import gzip
import hashlib
import pathlib
import platform as host_platform
import tarfile
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from unittest import mock

import httpx2
import pytest
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
) -> httpx2.Response:
    return httpx2.Response(
        status, stream=httpx2.ByteStream(content), headers=headers
    )


@contextmanager
def _http(
    respond: Callable[[httpx2.Request], httpx2.Response],
) -> Iterator[None]:
    client_type = httpx2.Client
    clients = []

    def client(**kwargs: Any) -> httpx2.Client:
        value = client_type(transport=httpx2.MockTransport(respond), **kwargs)
        clients.append(value)
        return value

    with mock.patch(
        "ggbuild.packages.sources.httpx2.Client", side_effect=client
    ):
        try:
            yield
        finally:
            assert clients
            assert all(value.is_closed for value in clients)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_download_retries_transient_http_errors(status: int) -> None:
    unavailable = _response(status)
    success = _response(200, b"archive")
    responses = iter((unavailable, success))
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.extensions["timeout"]["read"] == 30
        calls.append(str(request.url))
        return next(responses)

    with (
        _http(respond),
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
        mock.patch(
            "ggbuild.packages.sources.time.monotonic_ns", return_value=0
        ),
        package_sources._request_with_retry(  # ruff: ignore[private-member-access]
            "https://example.com/archive", stream=True
        ) as result,
    ):
        assert result is success
        assert result.read() == b"archive"

    assert calls == ["https://example.com/archive"] * 2
    sleep.assert_called_once_with(1.0)
    assert unavailable.is_closed
    assert success.is_closed


@pytest.mark.parametrize(
    "failure",
    [httpx2.ConnectError, httpx2.ReadTimeout, httpx2.RemoteProtocolError],
)
def test_download_retries_transport_errors(
    failure: type[httpx2.TransportError],
) -> None:
    attempts = 0

    def respond(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure("interrupted")
        return _response(200, b"archive")

    with (
        _http(respond),
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
        package_sources._request_with_retry(  # ruff: ignore[private-member-access]
            "https://example.com/archive", stream=False
        ) as response,
    ):
        assert response.content == b"archive"
    assert attempts == 2
    sleep.assert_called_once()


@pytest.mark.parametrize("transport_failure", [False, True])
def test_download_stops_retrying_after_five_attempts(
    *, transport_failure: bool
) -> None:
    responses = []
    attempts = 0

    def respond(_request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if transport_failure:
            raise httpx2.ConnectError("unreachable")
        response = _response(503)
        responses.append(response)
        return response

    expected_error = (
        httpx2.ConnectError if transport_failure else httpx2.HTTPStatusError
    )
    with (
        _http(respond),
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
        pytest.raises(expected_error),
        package_sources._request_with_retry(  # ruff: ignore[private-member-access]
            "https://example.com/archive", stream=True
        ),
    ):
        pytest.fail("the download should fail")
    assert attempts == 5
    assert sleep.call_count == 4
    assert all(response.is_closed for response in responses)


def test_download_retry_honors_bounded_retry_after() -> None:
    unavailable = _response(429, headers={"retry-after": "120"})
    success = _response(200, b"archive")
    responses = iter((unavailable, success))

    with (
        _http(lambda _request: next(responses)),
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
        package_sources._request_with_retry(  # ruff: ignore[private-member-access]
            "https://example.com/archive", stream=True
        ),
    ):
        pass

    sleep.assert_called_once_with(60.0)
    assert unavailable.is_closed
    assert success.is_closed


def test_download_falls_back_to_checksum_verified_mirror(
    tmp_path: pathlib.Path,
) -> None:
    expected = b"verified archive"
    primary = _response(200, b"wrong archive")
    mirror = _response(200, expected)
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return primary if request.url.host == "primary.example" else mirror

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

    with _http(respond):
        result = source._download(destination, mock.Mock())  # ruff: ignore[private-member-access]

    assert result.read_bytes() == expected
    assert calls == list(source.urls)
    assert primary.is_closed
    assert mirror.is_closed


def test_download_uses_mirror_without_retrying_not_found(
    tmp_path: pathlib.Path,
) -> None:
    not_found = _response(404)
    mirror = _response(200, b"archive")
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return not_found if request.url.host == "primary.example" else mirror

    source = package_sources.HttpsSource(
        "https://primary.example/archive.tar.gz",
        "archive.tar.gz",
        mirrors=("https://mirror.example/archive.tar.gz",),
    )

    with (
        _http(respond),
        mock.patch("ggbuild.packages.sources.time.sleep") as sleep,
    ):
        source._download(tmp_path / source.name, mock.Mock())  # ruff: ignore[private-member-access]

    assert calls == list(source.urls)
    sleep.assert_not_called()
    assert not_found.is_closed


def test_download_removes_partial_file_when_all_locations_fail(
    tmp_path: pathlib.Path,
) -> None:
    destination = tmp_path / "archive.tar.gz"

    class InterruptedStream(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"partial archive"
            assert destination.exists()
            raise httpx2.ReadError("disconnected")

    partial = httpx2.Response(200, stream=InterruptedStream())
    not_found = _response(404)

    def respond(request: httpx2.Request) -> httpx2.Response:
        return partial if request.url.host == "primary.example" else not_found

    source = package_sources.HttpsSource(
        "https://primary.example/archive.tar.gz",
        destination.name,
        mirrors=("https://mirror.example/archive.tar.gz",),
    )

    with (
        _http(respond),
        pytest.raises(RuntimeError, match="all download locations failed"),
    ):
        source._download(destination, mock.Mock())  # ruff: ignore[private-member-access]

    assert not destination.exists()
    assert partial.is_closed
    assert not_found.is_closed


def test_download_follows_redirects_and_fetches_checksum(
    tmp_path: pathlib.Path,
) -> None:
    expected = b"verified archive" * 1024
    checksum = hashlib.sha256(expected).hexdigest()
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        if request.url.host == "origin.example":
            return httpx2.Response(
                302, headers={"Location": "https://cdn.example/archive.tar.gz"}
            )
        if request.url.path.endswith(".sha256"):
            return _response(200, f"{checksum}  archive.tar.gz\n".encode())
        return _response(
            200, gzip.compress(expected), headers={"Content-Encoding": "gzip"}
        )

    source = package_sources.HttpsSource(
        "https://origin.example/archive.tar.gz", "archive.tar.gz"
    )
    source.add_verification(
        package_sources.HashVerification(
            "sha256", hash_url="https://cdn.example/archive.sha256"
        )
    )
    with _http(respond):
        result = source._download(tmp_path / source.name, mock.Mock())  # ruff: ignore[private-member-access]
    assert result.read_bytes() == expected
    assert calls == [
        "https://origin.example/archive.tar.gz",
        "https://cdn.example/archive.tar.gz",
        "https://cdn.example/archive.sha256",
    ]


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
