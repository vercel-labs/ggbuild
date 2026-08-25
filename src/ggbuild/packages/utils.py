# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import packaging.utils
from poetry.core.packages import dependency as poetry_dep


def python_dependency_from_pep_508(name: str) -> poetry_dep.Dependency:
    dep = poetry_dep.Dependency.create_from_pep_508(name)
    dep.__dict__["_name"] = packaging.utils.canonicalize_name(
        f"pypkg-{dep.name}"
    )
    dep.__dict__["_pretty_name"] = f"pypkg-{dep.pretty_name}"
    return dep
