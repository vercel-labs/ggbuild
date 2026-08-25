# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import ClassVar

from cleo.helpers import option

from ggbuild.planner import load_recipe
from ggbuild.project import load_project
from ggbuild.updater import update_releases
from ggbuild.workflow import check_generated, write_generated

from . import base


class Update(base.Command):
    name = "update"
    description = "Update registered releases using recipe policies."
    options: ClassVar = [
        option(
            "check", description="Fail when an update is available.", flag=True
        )
    ]

    def handle(self) -> int:
        config = load_project()
        load_recipe(config.root_recipe)
        check = bool(self.option("check"))
        result = update_releases(check=check, io=self.io)
        if check:
            check_generated(config)
            return 0
        for change in result.releases:
            old = ", ".join(change.previous)
            new = ", ".join(change.current)
            self.io.write_line(f"{change.package}: {old} -> {new}")
        for patch in result.patches:
            if patch.retained:
                self.io.write_line(f"patch retained: {patch.source.name}")
            elif patch.destination is not None:
                self.io.write_line(f"patch copied: {patch.destination.name}")
        write_generated(config)
        return 0
