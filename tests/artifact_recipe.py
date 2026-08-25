from __future__ import annotations

from typing import ClassVar

import shlex

from ggbuild import packages
from ggbuild.packages import sources as package_sources


class Tested(packages.BundledCAutoconfPackage):
    title, ident = "Tested", "tested"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/tested-{version}.tar.gz"}
    ]

    def get_test_script(self, test: packages.Test) -> str:
        payload = test.get_build_install_dir(self) / "payload"
        marker = test.get_temp_dir(self) / "hook-ran"
        return (
            f'test "$(cat {shlex.quote(str(payload))})" != fail\n'
            f"printf %s {shlex.quote(test.source_version)} > "
            f"{shlex.quote(str(marker))}"
        )


Tested("1.0", sha256="e" * 64)


class Untested(packages.BundledCAutoconfPackage):
    title, ident = "Untested", "untested"
    sources: ClassVar[list[str | package_sources.SourceDecl]] = [
        {"url": "https://example.com/untested-{version}.tar.gz"}
    ]


Untested("1.0", sha256="f" * 64)
