# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import TYPE_CHECKING

import platform

from . import (  # ruff: ignore[unused-import]
    deb,
    generic,
    linux,
    macos,
    rpm,
    win,
)
from .base import (
    AddUserAction,
    Build,
    BuildRequest,
    EnsureDirAction,
    InstallAspect,
    LinuxDistroTarget,
    LinuxTarget,
    Location,
    Target,
)
from .package import SystemPackage

if TYPE_CHECKING:
    from cleo.io.io import IO


__all__ = (
    "AddUserAction",
    "Build",
    "BuildRequest",
    "EnsureDirAction",
    "InstallAspect",
    "LinuxDistroTarget",
    "LinuxTarget",
    "Location",
    "SystemPackage",
    "Target",
)


def detect_target(
    io: IO,
    *,
    portable: bool,
    libc: str | None = None,
    arch: str | None = None,
) -> Target:
    target: Target
    system = platform.system()
    if arch is None:
        arch = platform.machine().lower()

    if arch == "amd64":
        arch = "x86_64"
    elif arch == "arm64":
        arch = "aarch64"

    if system == "Linux":
        if libc is None:
            libc = "gnu"
        target = linux.get_specific_target(libc, arch, portable=portable)

    elif system == "Darwin":
        v, _, _ = platform.mac_ver()
        version = tuple(int(p) for p in v.split("."))
        target = macos.get_specific_target(version, arch, portable=portable)

    elif system == "Windows":
        v = platform.version()
        version = tuple(int(p) for p in v.split("."))
        target = win.get_specific_target(version, arch, portable=portable)

    else:
        raise RuntimeError(f"System not supported: {system}")

    return target
