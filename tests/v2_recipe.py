# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import ClassVar

from poetry.core.packages import dependency as poetry_dependency

from ggbuild import packages
from ggbuild.packages import sources as package_sources


class Dependency(packages.BundledCAutoconfPackage):
    title, ident = "Dependency", "v2-dependency"
    aliases: ClassVar[list[str]] = ["v2-dependency-dev"]
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/dependency-{version}.tar.gz"}
    ]


Dependency("1.0", sha256="a" * 64)


class Root(packages.BundledCAutoconfPackage):
    title, ident = "Root", "v2-root"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/root-{version}.tar.gz"}
    ]
    artifact_requirements: ClassVar[packages.RequirementsSpec] = [
        poetry_dependency.Dependency("v2-dependency", "==1.0")
    ]
    artifact_build_requirements: ClassVar[packages.RequirementsSpec] = [
        poetry_dependency.Dependency("v2-dependency-dev", "==1.0")
    ]

    def get_test_script(self, test: packages.Test) -> str:
        del test
        return "true"


Root("1.1", sha256="b" * 64)
Root("1.2", sha256="c" * 64)
Root("2.0", sha256="d" * 64)
