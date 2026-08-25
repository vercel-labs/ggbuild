# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
)

from cleo.application import Application as BaseApplication
from cleo.formatters.style import Style
from lograil import configure_logging

import ggbuild

from . import commands as ggbuild_commands

if TYPE_CHECKING:
    from cleo.io.inputs.input import Input
    from cleo.io.io import IO
    from cleo.io.outputs.output import Output


class App(BaseApplication):
    def __init__(self) -> None:
        super().__init__(ggbuild.__name__, ggbuild.__version__)

    def create_io(
        self,
        input: Input | None = None,  # ruff: ignore[builtin-argument-shadowing] Cleo override
        output: Output | None = None,
        error_output: Output | None = None,
    ) -> IO:
        io = super().create_io(input, output, error_output)
        io.output.formatter.set_style("info", Style("blue").bold())
        io.error_output.formatter.set_style("info", Style("blue").bold())
        return io


# @lat: [[overview#System Intent]]
def main() -> int:
    configure_logging(logger_name="ggbuild")
    app = App()
    for cmd_name in ggbuild_commands.__all__:
        cmd = getattr(ggbuild_commands, cmd_name)
        app.add(cmd())

    return app.run()
