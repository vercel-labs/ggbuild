# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import contextlib
import importlib.resources
import os
import pathlib
import subprocess
from unittest import mock

import pytest

from ggbuild.targets import base as targets_base

# Tests intentionally exercise the internal lifecycle and wrapper boundary.
# ruff: file-ignore[private-member-access]


def _write_executable(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper")
def test_sccache_wrapper_delegates_compilers_and_avoids_recursion(
    tmp_path: pathlib.Path,
) -> None:
    wrapper = (
        pathlib.Path(str(importlib.resources.files("ggbuild.targets._helpers")))
        / "sccache-wrapper.sh"
    )
    wrapper_dir = tmp_path / "wrappers"
    real_dir = tmp_path / "real"
    wrapper_dir.mkdir()
    real_dir.mkdir()
    copied = wrapper_dir / "sccache-wrapper.sh"
    copied.write_bytes(wrapper.read_bytes())
    copied.chmod(0o755)
    log = tmp_path / "calls.log"
    fake_sccache = real_dir / "sccache-bin"
    _write_executable(
        fake_sccache,
        "#!/bin/sh\n"
        'printf "sccache:%s\\n" "$*" >> "$SCCACHE_TEST_LOG"\n'
        'exec "$@"\n',
    )
    names = ("cc", "c++", "gcc", "clang++", "x86_64-linux-gnu-gcc", "rustc")
    for name in names:
        _write_executable(
            real_dir / name,
            "#!/bin/sh\n"
            f'printf "compiler:{name}:%s\\n" "$*" '
            '>> "$SCCACHE_TEST_LOG"\n',
        )
        (wrapper_dir / name).symlink_to(copied)

    environment = {
        **os.environ,
        "PATH": os.pathsep.join((
            str(wrapper_dir),
            str(real_dir),
            os.environ.get("PATH", ""),
        )),
        "SCCACHE": str(fake_sccache),
        "SCCACHE_TEST_LOG": str(log),
    }
    for name in names:
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - test argv
            [wrapper_dir / name, "--fixture"],
            check=True,
            env=environment,
            shell=False,
        )

    records = log.read_text(encoding="utf-8").splitlines()
    assert records[::2] == [f"sccache:{name} --fixture" for name in names]
    assert records[1::2] == [f"compiler:{name}:--fixture" for name in names]


def _bare_build(tmp_path: pathlib.Path) -> targets_base.Build:
    build = object.__new__(targets_base.Build)
    build._enable_sccache = True
    build._droot = tmp_path
    build._system_tools = {}
    build._real_sccache_path = None
    build._sccache_configured = False
    build._sccache_started = False
    build._io = mock.Mock()
    return build


def test_unavailable_explicit_sccache_warns_and_disables(
    tmp_path: pathlib.Path,
) -> None:
    build = _bare_build(tmp_path)
    missing = tmp_path / "missing-sccache"

    with mock.patch.dict(os.environ, {"SCCACHE": str(missing)}, clear=False):
        build._find_sccache()

    assert build.is_sccache_enabled is False
    write_line = build._io.write_line
    assert isinstance(write_line, mock.Mock)
    warning = write_line.call_args.args[0]
    assert "continuing without compiler caching" in warning


def test_sccache_servers_are_isolated_by_build_directory(
    tmp_path: pathlib.Path,
) -> None:
    first = _bare_build(tmp_path / "first")
    second = _bare_build(tmp_path / "second")
    executable = tmp_path / "sccache"
    first._real_sccache_path = executable
    second._real_sccache_path = executable

    first_environment = first._sccache_environment()
    second_environment = second._sccache_environment()
    address_key = (
        "SCCACHE_SERVER_UDS"
        if "SCCACHE_SERVER_UDS" in first_environment
        else "SCCACHE_SERVER_PORT"
    )
    first_socket = first_environment[address_key]
    second_socket = second_environment[address_key]

    assert first_socket != second_socket
    assert "ggbuild-sccache" in first_socket or first_socket.isdecimal()


def test_stopping_sccache_reports_stats_before_shutdown(
    tmp_path: pathlib.Path,
) -> None:
    build = _bare_build(tmp_path)
    executable = tmp_path / "sccache"
    build._real_sccache_path = executable
    build._sccache_started = True

    with mock.patch("ggbuild.targets.base.subprocess.run") as run:
        run.return_value.returncode = 0
        build._stop_sccache()

    assert [call.args[0][1] for call in run.call_args_list] == [
        "--show-stats",
        "--stop-server",
    ]
    assert build._sccache_started is False


def test_failed_build_stops_sccache_server() -> None:
    class FailedBuild(targets_base.Build):
        def prepare(self) -> None:
            pass

        def build(self) -> None:
            raise RuntimeError("failed build")

        def _stop_sccache(self) -> None:
            self.stopped = True

    build = object.__new__(FailedBuild)
    build._root_pkg = mock.Mock()
    build.stopped = False

    with (
        mock.patch(
            "ggbuild.targets.base.stage",
            return_value=contextlib.nullcontext(),
        ),
        pytest.raises(RuntimeError, match="failed build"),
    ):
        build.run()

    assert build.stopped is True
