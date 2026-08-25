from __future__ import annotations

from typing import ClassVar

from ggbuild import packages
from ggbuild.packages import sources as package_sources


class PatchPackage(packages.BundledCAutoconfPackage):
    title, ident = "Patch package", "patch-package"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/patch-package-{version}.tar.gz"}
    ]


PatchPackage("17.10", sha256="a" * 64)
PatchPackage("18.4", sha256="b" * 64)
