# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import ClassVar

from ggbuild import packages
from ggbuild.packages import sources as package_sources


class RegisteredRelease(packages.BundledPackage):
    title = "Registered release"
    ident = "registered-release-command"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/package-{version}.tar.gz"}
    ]


RegisteredRelease(
    "1.2.3",
    sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
)
