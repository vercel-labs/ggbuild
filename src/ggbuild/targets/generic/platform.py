# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
)

import pathlib

from poetry.repositories import repository as poetry_repo

from ggbuild.targets import base as targets, package as tgt_pkg

from . import build as genbuild

if TYPE_CHECKING:
    from poetry.core.packages import (
        dependency as poetry_dep,
        package as poetry_pkg,
    )


Build = genbuild.Build


class GenericOSRepository(poetry_repo.Repository):
    def __init__(
        self, name: str, packages: list[poetry_pkg.Package] | None = None
    ) -> None:
        super().__init__(name, packages)
        self._pkg_impls: dict[str, type[tgt_pkg.SystemPackage]] = {}

    def list_provided_packages(self) -> frozenset[str]:
        # A list of packages assumed to be present on the system.
        return frozenset((
            "bison",
            "flex",
            "perl",
            "pkg-config",
        ))

    def register_package_impl(
        self,
        name: str,
        impl_cls: type[tgt_pkg.SystemPackage],
    ) -> None:
        self._pkg_impls[name] = impl_cls

    def find_packages(
        self,
        dependency: poetry_dep.Dependency,
    ) -> list[poetry_pkg.Package]:
        if dependency.name in self.list_provided_packages():
            impl_cls = self._pkg_impls.get(
                dependency.name, tgt_pkg.SystemPackage
            )
            pkg = impl_cls(
                dependency.name,
                version="999.0",
                pretty_version="999.0",
                system_name=dependency.name,
            )
            if not dependency.constraint.allows(pkg.version):
                return []
            self.add_package(pkg)

            return [pkg]
        return []


class GenericTarget(targets.FHSTarget):
    @property
    def name(self) -> str:
        return "Generic POSIX"

    def get_package_repository(self) -> GenericOSRepository:
        return GenericOSRepository("generic")

    def get_bundle_install_root(self, build: targets.Build) -> pathlib.Path:
        return pathlib.Path("/opt")

    def get_bundle_install_subdir(self, build: targets.Build) -> pathlib.Path:
        return build.root_package.get_root_install_subdir(build)

    def get_install_path(
        self,
        build: targets.Build,
        root: pathlib.Path,
        root_subdir: pathlib.Path,
        prefix: pathlib.Path,
        aspect: targets.InstallAspect,
    ) -> pathlib.Path:
        if aspect == "systembin":
            if root == pathlib.Path("/"):
                return root / "usr" / "bin"
            return root / "bin"
        paths = {
            "sysconf": prefix / "etc",
            "userconf": pathlib.Path("$HOME") / ".config",
            "data": prefix / "share",
            "legal": prefix / "licenses",
            "doc": prefix / "share" / "doc" / root_subdir,
            "info": prefix / "share" / "info",
            "man": prefix / "share" / "man",
            "bin": prefix / "bin",
            "lib": prefix / "lib",
            "include": prefix / "include",
            "localstate": root / "var",
            "runstate": root / "var" / "run",
        }
        try:
            return paths[aspect]
        except KeyError as error:
            raise LookupError(f"aspect: {aspect}") from error

    def get_builder(self) -> type[genbuild.Build]:
        return genbuild.Build
