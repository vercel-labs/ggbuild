# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import logging
import os
import os.path
import pathlib

from ggbuild import tools
from ggbuild.targets import generic

logger = logging.getLogger(__name__)


class GenericLinuxBuild(generic.Build):
    def get_tool_list(self) -> list[str]:
        tools = super().get_tool_list()
        tools.append("linux-static-linkdriver-wrapper.sh")
        return tools

    def _get_global_env_vars(self) -> dict[str, str]:
        env = super()._get_global_env_vars()

        wrapper = self.sh_get_command(
            "linux-static-linkdriver-wrapper",
            relative_to="sourceroot",
        )

        target = self.target.triple.upper().replace("-", "_")
        env[f"CARGO_TARGET_{target}_LINKER"] = f"$(ROOT)/{wrapper}"

        return env

    def _fixup_rpath(
        self,
        image_root: pathlib.Path,
        binary_relpath: pathlib.Path,
        *,
        additional_rpaths: set[pathlib.Path] | None = None,
    ) -> None:
        inst_prefix = self.get_bundle_install_prefix()
        full_path = image_root / binary_relpath
        inst_path = pathlib.Path("/") / binary_relpath
        rpath_record = tools.cmd("patchelf", "--print-rpath", full_path).strip()
        rpaths = []
        raw_rpaths = rpath_record.split(os.pathsep) if rpath_record else []
        raw_rpaths.extend(map(str, additional_rpaths or set()))
        if raw_rpaths:
            for raw_entry in raw_rpaths:
                entry = raw_entry.strip()
                if not entry:
                    continue

                if entry.startswith("$ORIGIN"):
                    # rpath is already relative
                    rpaths.append(entry)
                else:
                    rpath = pathlib.Path(entry)
                    if rpath.is_relative_to(inst_prefix):
                        rel_rpath = os.path.relpath(
                            rpath, start=inst_path.parent
                        )
                        rpaths.append(f"$ORIGIN/{rel_rpath}")
                    else:
                        logger.info(
                            "RPATH %s is outside the install image; removing",
                            entry,
                        )

        if rpaths:
            new_rpath_record = os.pathsep.join(rp for rp in rpaths)
            if new_rpath_record != rpath_record:
                tools.cmd(
                    "patchelf",
                    "--force-rpath",
                    "--set-rpath",
                    new_rpath_record,
                    full_path,
                )
        elif rpath_record:
            tools.cmd(
                "patchelf",
                "--remove-rpath",
                full_path,
            )

    def _strip(
        self, image_root: pathlib.Path, binary_relpath: pathlib.Path
    ) -> None:
        full_path = image_root / binary_relpath
        tools.cmd("strip", full_path)
