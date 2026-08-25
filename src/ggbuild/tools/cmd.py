# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import Any, cast

import logging
import os
import pathlib
import subprocess
import sys

from ggbuild.exceptions import GGBuildRuntimeError

logger = logging.getLogger(__name__)


def cmd(
    *cmd: str | os.PathLike[str],
    errors_are_fatal: bool = True,
    hide_stderr: bool = False,
    error_context: str | None = None,
    **kwargs: Any,
) -> str:
    default_kwargs: dict[str, Any] = {
        "stderr": subprocess.DEVNULL if hide_stderr else sys.stderr,
        "stdout": subprocess.PIPE,
    }

    default_kwargs.update(kwargs)

    str_cmd = [str(c) for c in cmd]
    cmd_line = " ".join(str_cmd)
    cwd = kwargs.get("cwd") or pathlib.Path.cwd()
    logger.info("%s> %s", cwd, cmd_line)

    try:
        p = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] argv only
            str_cmd, text=True, check=True, **default_kwargs
        )
    except subprocess.CalledProcessError as e:
        if errors_are_fatal:
            output = e.stdout or e.stderr
            if output:
                sys.stderr.write(f"{output.rstrip()}\n")
            raise GGBuildRuntimeError.create(
                reason=error_context or f"{cmd[0]} failed",
                exception=e,
            ) from e
        raise
    else:
        output = p.stdout
        if output is not None:
            output = output.rstrip()
        return cast("str", output)
