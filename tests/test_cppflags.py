# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import json
import os
import pathlib
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ggbuild.app  # ruff: ignore[unused-import]  # Initialize import graph.
from ggbuild import packages, targets


class _AutoconfFixture(packages.BundledCAutoconfPackage):
    title = "Autoconf fixture"
    ident = "autoconf-fixture"


class BundledCppflagsTest(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "exercises POSIX shell command syntax")
    def test_make_build_command_accepts_ordered_targets(self) -> None:
        package = object.__new__(_AutoconfFixture)
        build = mock.create_autospec(targets.Build, instance=True)
        build.build_parallelism = 4
        build.sh_format_args.return_value = ""
        build.sh_format_command.return_value = "env"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            make = tmp / "make"
            make.write_text(
                f"""#!{sys.executable}
import json
import pathlib
import sys
pathlib.Path({str(trace)!r}).write_text(json.dumps(sys.argv[1:]))
""",
                encoding="utf-8",
            )
            make.chmod(0o755)
            build.sh_get_command.return_value = str(make)

            subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
                [
                    "/bin/sh",
                    "-eu",
                    "-c",
                    package.get_build_command(
                        build,
                        {},
                        ("-C", "src/test", "all"),
                    ),
                ],
                check=True,
                cwd=tmp,
            )

            self.assertEqual(
                json.loads(trace.read_text(encoding="utf-8")),
                ["-C", "src/test", "all"],
            )

    def test_autoconf_configure_uses_selected_bash(self) -> None:
        package = object.__new__(_AutoconfFixture)
        build = mock.create_autospec(targets.Build, instance=True)
        build.get_source_dir.return_value = pathlib.Path(
            "/source/autoconf-fixture"
        )
        build.sh_get_command.return_value = "/selected/bash"

        command = package.sh_get_configure_command(build)

        self.assertEqual(
            command,
            "/selected/bash /source/autoconf-fixture/configure",
        )
        build.sh_get_command.assert_called_once_with("bash")

    def test_autoconf_propagates_selected_bash_as_config_shell(self) -> None:
        package = object.__new__(_AutoconfFixture)
        build = mock.create_autospec(targets.Build, instance=True)
        build.get_build_reqs.return_value = []
        build.sh_append_global_flags.return_value = {}
        build.get_ld_env.return_value = {}
        build.sh_get_command.return_value = "/selected/bash"

        env = package.get_configure_env(build, wd="${_wd}")

        self.assertEqual(env["CONFIG_SHELL"], "/selected/bash")
        if platform.system() == "Darwin":
            self.assertEqual(env["lt_cv_sys_max_cmd_len"], "1048576")
        build.sh_get_command.assert_called_once_with("bash")

    def test_cppflags_helper_only_returns_include_search_path(self) -> None:
        build = mock.create_autospec(targets.Build, instance=True)
        dependency = mock.create_autospec(
            packages.BasePackage,
            instance=True,
        )
        build.sh_get_bundled_pkg_include_path.return_value = "/staged/include"

        flags = targets.Build.sh_get_bundled_pkg_cppflags(
            build,
            dependency,
            wd="${_wd}",
        )

        self.assertEqual(flags, ["-I/staged/include"])
        build.sh_get_bundled_pkg_include_path.assert_called_once_with(
            dependency,
            "pkgbuild",
            wd="${_wd}",
            relative_to_package=None,
        )

    def test_autoconf_dependency_adds_include_path_to_cppflags(self) -> None:
        package = object.__new__(_AutoconfFixture)
        dependency = mock.create_autospec(
            packages.BasePackage,
            instance=True,
        )
        build = mock.create_autospec(targets.Build, instance=True)
        build.is_bundled.return_value = True
        build.sh_get_bundled_install_path.return_value = "/staged"
        build.sh_get_bundled_pkg_cppflags.return_value = ["-I/staged/include"]
        build.sh_get_bundled_pkg_bin_path.return_value = None
        conf_args: packages.Args = {}
        conf_env: packages.Args = {}
        metadata = packages.PkgConfigMeta(
            pkg_name="FIXTURE",
            provides_pkg_config=True,
            provides_shlibs=True,
            provides_c_headers=True,
        )

        with (
            mock.patch(
                "ggbuild.packages.base.platform.system", return_value="Linux"
            ),
            mock.patch.object(
                _AutoconfFixture,
                "get_dep_pkg_config_meta",
                return_value=metadata,
            ),
        ):
            package.configure_dependency(
                build,
                dependency,
                conf_args,
                conf_env,
                wd="${_wd}",
            )

        build.sh_get_bundled_pkg_cppflags.assert_called_once_with(
            dependency,
            wd="${_wd}",
        )
        build.sh_append_quoted_flags.assert_called_once_with(
            conf_env,
            "CPPFLAGS",
            ["-I/staged/include"],
        )
        self.assertNotIn("CFLAGS", conf_env)


if __name__ == "__main__":
    unittest.main()
