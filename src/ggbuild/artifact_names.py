# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Canonical public artifact names."""

from __future__ import annotations

from typing import Literal

import re

type PublicArtifactRole = Literal["primary-archive", "dbgsym"]


def validate_publication_tag(tag: str) -> str:
    """Return a valid UTC-minute publication tag."""
    if re.fullmatch(r"[0-9]{12}", tag) is None:
        raise ValueError("publication tag must contain exactly twelve digits")
    return tag


def public_artifact_stem(
    package: str,
    source_version: str,
    target: str,
    tag: str,
    *,
    role: PublicArtifactRole = "primary-archive",
) -> str:
    """Return the public filename stem for one artifact role."""
    validate_publication_tag(tag)
    stem = f"{package}-{source_version}+{tag}-{target}"
    return f"{stem}-dbgsym" if role == "dbgsym" else stem


def public_artifact_name(
    package: str,
    source_version: str,
    target: str,
    tag: str,
    *,
    role: PublicArtifactRole = "primary-archive",
) -> str:
    """Return the canonical public zstd-tar filename."""
    return (
        public_artifact_stem(
            package,
            source_version,
            target,
            tag,
            role=role,
        )
        + ".tar.zst"
    )
