# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

import os
import pathlib

CACHEHOME = os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")
CACHEDIR = pathlib.Path(CACHEHOME) / "ggbuild"


def cachedir() -> pathlib.Path:
    CACHEDIR.mkdir(parents=True, exist_ok=True)

    return CACHEDIR
