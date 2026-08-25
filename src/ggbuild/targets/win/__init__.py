# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from .platform import (
    ModernWindowsPortableTarget,
    ModernWindowsTarget,
    WindowsTarget,
    get_specific_target,
)

__all__ = (
    "ModernWindowsPortableTarget",
    "ModernWindowsTarget",
    "WindowsTarget",
    "get_specific_target",
)
