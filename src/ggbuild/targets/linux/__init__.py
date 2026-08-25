# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from .platform import (
    LinuxGenericTarget,
    LinuxMuslTarget,
    LinuxPortableMuslTarget,
    get_specific_target,
)

__all__ = (
    "LinuxGenericTarget",
    "LinuxMuslTarget",
    "LinuxPortableMuslTarget",
    "get_specific_target",
)
