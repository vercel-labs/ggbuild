# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from ggbuild.packages import base as mpkg_base

from . import base

if TYPE_CHECKING:
    from poetry.core.constraints.version import Version


class SystemPackage(mpkg_base.PackageWithPrettyVersion):
    def __init__(
        self,
        name: str,
        version: str | Version,
        pretty_version: str | None = None,
        system_name: str | None = None,
    ) -> None:
        super().__init__(name, version, pretty_version=pretty_version)
        self._system_name = system_name

    @property
    def system_name(self) -> str | None:
        return self._system_name

    def get_shlibs(self, build: base.Build) -> list[str]:
        return []

    def __repr__(self) -> str:
        return f"<SystemPackage {self.unique_name}>"


SystemPackage_T = TypeVar("SystemPackage_T", bound=SystemPackage)


class StandardSystemPackage(SystemPackage):
    """A package that is part of standard system distribution.

    Standard packages take precendnce over those can be installed by a user
    or the package manager and that can be located via pkg-config.
    """
