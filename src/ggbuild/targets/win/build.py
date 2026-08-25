# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

import pathlib
import shutil

from ggbuild import packages
from ggbuild.targets import generic


class Build(generic.Build):
    def _get_exposed_command_script(
        self,
        pkg: packages.BasePackage,
        cmd: pathlib.Path,
        *,
        install_dir: pathlib.Path,
        bindir: pathlib.Path,
    ) -> tuple[str, pathlib.Path]:
        suffix = self.get_exe_suffix()
        source = cmd.with_name(f"{cmd.name}{suffix}").relative_to("/")
        command_name = f"{cmd.name}{pkg.slot_suffix}{suffix}"
        installed = bindir / command_name
        return (
            f'cp -p "{install_dir / source}" "{install_dir / installed}"',
            installed,
        )

    def define_tools(self) -> None:
        super().define_tools()
        # "realbash" below is to circumvent a dubious practice
        # of Windows intercepting bare invocations of "bash" to mean
        # "WSL", since make runs its shells using bare names even
        # if SHELL contains a fully-qualified path.
        self._system_tools["bash"] = "realbash"
        self._system_tools["python"] = "python"
        find = shutil.which("find")
        if find is None:
            raise RuntimeError("could not locate `find`")
        self._system_tools["find"] = find
        tar = shutil.which("tar")
        if tar is None:
            raise RuntimeError("could not locate `tar`")
        self._system_tools["tar"] = tar
        self._system_tools["meson"] = "meson"
        self._system_tools["cmake"] = "cmake"
        self._system_tools["ninja"] = "ninja"
