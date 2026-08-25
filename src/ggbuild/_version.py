# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

import importlib.metadata

try:
    __version__ = importlib.metadata.version("ggbuild")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0+unknown"
