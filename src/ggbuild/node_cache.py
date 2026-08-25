# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence


def policy_for(
    policies: Mapping[str, str], aliases: Sequence[str]
) -> str | None:
    """Resolve cache policy using the last matching rule."""
    result: str | None = None
    for pattern, policy in policies.items():
        if any(fnmatch.fnmatchcase(alias, pattern) for alias in aliases):
            result = policy
    return result


def unmatched_patterns(
    policies: Mapping[str, str], known: Sequence[str] | set[str]
) -> list[str]:
    """Return policy names or patterns which match no known node alias."""
    return sorted(
        pattern
        for pattern in policies
        if not any(fnmatch.fnmatchcase(value, pattern) for value in known)
    )
