from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import sys
import tarfile
from unittest import mock

import pytest
import zstandard

import ggbuild.app  # ruff: ignore[unused-import]  # Initialize import graph.
from ggbuild.packages import BundledPackage
from ggbuild.targets.generic.build import Build


def _test_commands(copy_tree: pathlib.Path) -> tuple[str, str]:
    shell = shutil.which("bash") or shutil.which("sh")
    if shell is None:
        pytest.skip("test data staging requires a POSIX shell")
    return shlex.join([shell]), shlex.join([sys.executable, str(copy_tree)])


def _extract_zstd_tar(archive: pathlib.Path, destination: pathlib.Path) -> None:
    raw = destination / "raw.tar"
    with archive.open("rb") as source, raw.open("wb") as target:
        zstandard.ZstdDecompressor().copy_stream(source, target)
    with tarfile.open(raw) as source:
        source.extractall(destination, filter="data")


def test_test_list_side_data_has_role_metadata(
    tmp_path: pathlib.Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "schedule").write_text("test", encoding="utf-8")
    build = object.__new__(Build)
    build._root_pkg = mock.Mock()  # ruff: ignore[private-member-access]

    with (
        mock.patch.object(build, "_test_data_root", return_value=source),
    ):
        refs, contents = build._package_side_artifacts(  # ruff: ignore[private-member-access]
            "fixture", tmp_path / "output", dbgsym_root=None
        )

    assert refs == ["fixture.test-data.tar.zst"]
    assert contents[refs[0]]["artifact_role"] == "test-data"
    assert contents[refs[0]]["overlay"] is True
    assert contents[refs[0]]["root"] == ".ggbuild-test-data"
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    _extract_zstd_tar(tmp_path / "output" / refs[0], extracted)
    assert (
        extracted / "fixture/.ggbuild-test-data/schedule"
    ).read_text() == "test"


def test_test_list_stages_only_test_install_paths(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "workspace"
    test_install = workspace / "_artifacts/test-install/postgresql"
    test_install.mkdir(parents=True)
    (test_install / "manifest.json").write_text("{}", encoding="utf-8")
    temporary = workspace / "_artifacts/tmp"
    package_temp = temporary / "postgresql"
    package_temp.mkdir(parents=True)
    copy_tree = (
        pathlib.Path(__file__).parents[1]
        / "src/ggbuild/targets/_helpers/copy-tree.py"
    )
    shell, copy = _test_commands(copy_tree)
    build = object.__new__(Build)
    build._root_pkg = mock.Mock()  # ruff: ignore[private-member-access]

    def command(name: str, **kwargs: object) -> str:
        del kwargs
        return shell if name == "bash" else copy

    with (
        mock.patch.object(
            build,
            "render_package_script",
            return_value="printf '%s\\n' manifest.json",
        ) as render_package_script,
        mock.patch.object(
            build,
            "get_test_install_dir",
            return_value=test_install,
        ),
        mock.patch.object(build, "get_temp_root", return_value=temporary),
        mock.patch.object(build, "get_temp_dir", return_value=package_temp),
        mock.patch.object(build, "sh_get_command", side_effect=command),
        mock.patch.object(
            build, "_postprocess_test_data_root", side_effect=lambda path: path
        ),
    ):
        root = build._test_data_root()  # ruff: ignore[private-member-access]

    assert root == temporary / "test-data"
    assert (root / "manifest.json").is_file()
    render_package_script.assert_called_once_with(
        build._root_pkg,  # ruff: ignore[private-member-access]
        "test_install_list",
        relative_to="fsroot",
    )


def test_test_list_preserves_relative_test_install_symlinks(
    tmp_path: pathlib.Path,
) -> None:
    workspace = tmp_path / "workspace"
    test_install = workspace / "_artifacts/test-install/postgresql"
    test_install.mkdir(parents=True)
    (test_install / "runner").write_text("runner", encoding="utf-8")
    try:
        (test_install / "runner-link").symlink_to("runner")
    except OSError:
        pytest.skip("relative symlinks are unavailable")
    temporary = workspace / "_artifacts/tmp"
    package_temp = temporary / "postgresql"
    package_temp.mkdir(parents=True)
    copy_tree = (
        pathlib.Path(__file__).parents[1]
        / "src/ggbuild/targets/_helpers/copy-tree.py"
    )
    shell, copy = _test_commands(copy_tree)
    build = object.__new__(Build)
    build._root_pkg = mock.Mock()  # ruff: ignore[private-member-access]

    def command(name: str, **kwargs: object) -> str:
        del kwargs
        return shell if name == "bash" else copy

    with (
        mock.patch.object(
            build,
            "render_package_script",
            return_value="printf '%s\\n' runner runner-link",
        ),
        mock.patch.object(
            build, "get_test_install_dir", return_value=test_install
        ),
        mock.patch.object(build, "get_temp_root", return_value=temporary),
        mock.patch.object(build, "get_temp_dir", return_value=package_temp),
        mock.patch.object(build, "sh_get_command", side_effect=command),
        mock.patch.object(
            build, "_postprocess_test_data_root", side_effect=lambda path: path
        ),
    ):
        root = build._test_data_root()  # ruff: ignore[private-member-access]

    assert root is not None
    assert (root / "runner-link").is_symlink()
    assert (root / "runner-link").read_text(encoding="utf-8") == "runner"


def test_test_install_list_uses_dedicated_staging_root(
    tmp_path: pathlib.Path,
) -> None:
    test_install = tmp_path / "test-install"
    test_install.mkdir()
    package = mock.Mock(spec=BundledPackage, unique_name="postgresql-17.10")
    package.get_file_test_install_entries.return_value = [
        "manifest.json",
        "bin/**",
    ]
    package.write_file_list_script.side_effect = (
        lambda _build, _name, entries, **kwargs: repr((entries, kwargs))
    )
    build = mock.Mock()
    build.get_test_install_dir.return_value = test_install

    BundledPackage.get_test_install_list_script(package, build)

    package.write_file_list_script.assert_called_once_with(
        build,
        "test-install",
        ["manifest.json", "bin/**"],
        imply_parents=True,
        root=test_install,
    )


def test_test_data_uses_artifact_image_binary_postprocessing(
    tmp_path: pathlib.Path,
) -> None:
    image = tmp_path / "image"
    shipped_library = image / "opt/package/lib/library.dylib"
    shipped_library.parent.mkdir(parents=True)
    shipped_library.write_bytes(b"library")
    staged = tmp_path / "staged"
    harness = staged / "build/tests/harness"
    harness.parent.mkdir(parents=True)
    harness.write_bytes(b"harness")
    temporary = tmp_path / "temporary"
    package = mock.Mock(name_slot="package")
    build = object.__new__(Build)
    build._root_pkg = package  # ruff: ignore[private-member-access]

    with (
        mock.patch.object(build, "get_temp_root", return_value=temporary),
        mock.patch.object(build, "get_image_root", return_value=image),
        mock.patch.object(
            build,
            "get_bundle_install_prefix",
            return_value=pathlib.Path("/opt/package"),
        ),
        mock.patch.object(build, "_fixup_binaries") as fixup,
    ):
        root = build._postprocess_test_data_root(  # ruff: ignore[private-member-access]
            staged
        )

    assert root == temporary / "test-data-image/opt/package/.ggbuild-test-data"
    assert (root / "build/tests/harness").read_bytes() == b"harness"
    assert (
        temporary / "test-data-image/opt/package/lib/library.dylib"
    ).read_bytes() == b"library"
    files = fixup.call_args.args[0]
    assert pathlib.Path("opt/package/lib/library.dylib") in files
    assert (
        pathlib.Path("opt/package/.ggbuild-test-data/build/tests/harness")
        in files
    )
    assert fixup.call_args.kwargs == {
        "image_root": temporary / "test-data-image",
        "additional_rpath_scope": pathlib.Path(
            "opt/package/.ggbuild-test-data"
        ),
        "additional_rpaths": {
            pathlib.Path("/opt/package/.ggbuild-test-data/lib")
        },
    }


def test_binary_postprocessing_preserves_build_timestamps(
    tmp_path: pathlib.Path,
) -> None:
    image = tmp_path / "image"
    binary = image / "build/test-driver"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"unprocessed")
    original_mtime = 1_700_000_000_123_456_789
    binary.touch()
    binary_stat = binary.stat()
    binary.chmod(binary_stat.st_mode)
    os.utime(binary, ns=(binary_stat.st_atime_ns, original_mtime))
    stored_mtime = binary.stat().st_mtime_ns

    build = object.__new__(Build)
    build._build_dbgsym = False  # ruff: ignore[private-member-access]
    build._target = mock.Mock()  # ruff: ignore[private-member-access]
    build._target.is_binary_code_file.return_value = True  # ruff: ignore[private-member-access]
    build._target.is_dynamically_linked.return_value = False  # ruff: ignore[private-member-access]

    def strip(_root: pathlib.Path, relative: pathlib.Path) -> None:
        (image / relative).write_bytes(b"processed")

    with mock.patch.object(build, "_strip", side_effect=strip):
        build._collect_binary_refs(  # ruff: ignore[private-member-access]
            [pathlib.Path("build/test-driver")], image
        )

    assert binary.stat().st_mtime_ns == stored_mtime


def test_dbgsym_root_extracts_symbols_and_strips_primary(
    tmp_path: pathlib.Path,
) -> None:
    image = tmp_path / "image"
    binary = image / "bin/program"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    build = object.__new__(Build)
    build._build_dbgsym = True  # ruff: ignore[private-member-access]
    build._target = mock.Mock(  # ruff: ignore[private-member-access]
        triple="x86_64-unknown-linux-gnu"
    )
    build._target.is_binary_code_file.return_value = True  # ruff: ignore[private-member-access]
    calls: list[tuple[pathlib.Path, pathlib.Path]] = []

    def extract(source: pathlib.Path, destination: pathlib.Path) -> None:
        calls.append((source, destination))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"symbols")

    with (
        mock.patch.object(build, "get_image_root", return_value=image),
        mock.patch.object(
            build,
            "get_temp_root",
            return_value=tmp_path / "temporary",
        ),
        mock.patch.object(build, "_extract_dbgsym", side_effect=extract),
    ):
        root = build._dbgsym_artifact_root()  # ruff: ignore[private-member-access]

    assert root == tmp_path / "temporary/dbgsym"
    assert calls == [(binary, root / "bin/program")]
