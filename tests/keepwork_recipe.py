# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pathlib

from ggbuild import packages, targets

if TYPE_CHECKING:
    from ggbuild.packages import sources as package_sources


class KeepworkRecipe(packages.BundledPackage):
    title = "ggbuild keepwork integration fixture"
    ident = "ggbuild-keepwork-fixture"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = []

    @classmethod
    def _get_sources(
        cls,
        version: str | None,
    ) -> list[packages.BaseSource]:
        if version != "1.0":
            raise ValueError("fixture version must be 1.0")
        source = (pathlib.Path(__file__).parent / "keepwork_source").resolve()
        return [packages.sources.source_for_url(source.as_uri())]

    def get_prepare_script(self, build: targets.Build) -> str:
        return 'echo prepare >> "$GGBUILD_KEEPWORK_TRACE"\n'

    def get_configure_script(self, build: targets.Build) -> str:
        source = build.get_source_dir(self, relative_to="pkgbuild")
        return (
            'echo configure >> "$GGBUILD_KEEPWORK_TRACE"\n'
            f'cp "{source}/Makefile" .\n'
            f'cp "{source}/hello.c" .\n'
        )

    def get_build_script(self, build: targets.Build) -> str:
        return 'echo build >> "$GGBUILD_KEEPWORK_TRACE"\nmake\n'

    def get_build_install_script(self, build: targets.Build) -> str:
        destination = build.get_build_install_dir(
            self,
            relative_to="pkgbuild",
        )
        bindir = self.get_install_path(build, "bin")
        if bindir is None:
            raise RuntimeError("fixture has no binary install path")
        executable = f"hello{build.get_exe_suffix()}"
        return (
            super().get_build_install_script(build)
            + '\necho build_install >> "$GGBUILD_KEEPWORK_TRACE"\n'
            + 'if [ -e "$GGBUILD_KEEPWORK_FAIL_ONCE" ]; then\n'
            + '  rm "$GGBUILD_KEEPWORK_FAIL_ONCE"\n'
            + "  exit 23\n"
            + "fi\n"
            + f'mkdir -p "{destination / bindir.relative_to("/")}"\n'
            + f'cp "{executable}" '
            + f'"{destination / bindir.relative_to("/")}/{executable}"\n'
        )

    @property
    def provides_build_tools(self) -> bool:
        return True

    def get_file_install_entries(self, build: targets.Build) -> list[str]:
        return ["{bindir}/hello{exesuffix}"]

    def get_exposed_commands(self, build: targets.Build) -> list[pathlib.Path]:
        bindir = self.get_install_path(build, "bin")
        if bindir is None:
            raise RuntimeError("fixture has no binary install path")
        return [bindir / "hello"]
