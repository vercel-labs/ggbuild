# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import ClassVar

import pathlib
import shutil

from cleo.helpers import argument, option

from ggbuild.testing import run_test

from . import base


class Test(base.Command):
    name = "test"
    description = "Tests a built artifact with its recipe hook."
    arguments: ClassVar = [argument("archive", description="Artifact archive")]
    options: ClassVar = [
        option(
            "work-dir",
            description="Isolated directory for test inputs and state.",
            flag=False,
        ),
        option(
            "recipe", description="Exact root recipe reference.", flag=False
        ),
        option("result", description="Write the test result here.", flag=False),
        option(
            "bare-linux",
            description="Run the test hook in the target's bare Linux image.",
            flag=True,
        ),
    ]

    def handle(self) -> int:
        work_dir = self.option("work-dir")
        _, result_path = run_test(
            pathlib.Path(self.argument("archive")),
            work_dir=pathlib.Path(work_dir) if work_dir else None,
            recipe_reference=self.option("recipe"),
            bare_linux=bool(self.option("bare-linux")),
        )
        if destination := self.option("result"):
            destination_path = pathlib.Path(destination)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(result_path, destination_path)
            result_path = destination_path
        self.io.write_line(str(result_path))
        return 0
