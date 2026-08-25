# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from .platform import (
    BaseDebTarget,
    DebianStretchOrNewerTarget,
    DebRepository,
    ModernDebianTarget,
    UbuntuBionicOrNewerTarget,
    UbuntuXenialOrNewerTarget,
    get_specific_target,
)

__all__ = (
    "BaseDebTarget",
    "DebRepository",
    "DebianStretchOrNewerTarget",
    "ModernDebianTarget",
    "UbuntuBionicOrNewerTarget",
    "UbuntuXenialOrNewerTarget",
    "get_specific_target",
)
