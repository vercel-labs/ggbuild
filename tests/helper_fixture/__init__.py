from __future__ import annotations

from typing import ClassVar

from ggbuild import packages
from ggbuild.packages import sources as package_sources


class HelperPackage(packages.BundledCAutoconfPackage):
    title, ident = "Helper package", "helper-package"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/helper-package-{version}.tar.gz"}
    ]


HelperPackage("1.0", sha256="a" * 64)
