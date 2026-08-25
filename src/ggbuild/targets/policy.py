# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Default project execution policy derived from target triples."""

from __future__ import annotations

from typing import Literal

import dataclasses

from ggbuild.targets.linux.dockerfile import docker_environment

ExecutionMode = Literal["host", "docker"]


@dataclasses.dataclass(frozen=True, slots=True)
class TargetPolicy:
    triple: str
    execution: ExecutionMode
    runner: str
    environment: tuple[tuple[str, str], ...] = ()


def target_policy(triple: str) -> TargetPolicy:
    """Return ggbuild's defaults for a supported target triple."""
    arch = triple.partition("-")[0]
    if triple.endswith(("-unknown-linux-gnu", "-unknown-linux-musl")):
        docker_environment(triple)
        runners = {
            "aarch64": "ubuntu-24.04-arm",
            "x86_64": "ubuntu-latest",
        }
        try:
            runner = runners[arch]
        except KeyError as error:
            raise ValueError(
                f"target {triple} has no default Actions runner"
            ) from error
        return TargetPolicy(triple, "docker", runner)
    if triple.endswith("-apple-darwin"):
        runners = {
            "aarch64": "macos-latest",
            "x86_64": "macos-15-intel",
        }
        try:
            runner = runners[arch]
        except KeyError as error:
            raise ValueError(
                f"target {triple} has no default Actions runner"
            ) from error
        return TargetPolicy(
            triple,
            "host",
            runner,
            environment=(("MACOSX_DEPLOYMENT_TARGET", "13.0"),),
        )
    if triple == "x86_64-pc-windows-msvc":
        return TargetPolicy(triple, "host", "windows-latest")
    raise ValueError(f"unsupported project target: {triple}")
