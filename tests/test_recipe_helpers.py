from __future__ import annotations

import os
import pathlib
from unittest import mock

import ggbuild.app  # ruff: ignore[unused-import]  # Initialize import graph.
from tests.helper_fixture import HelperPackage


def test_recipe_helpers_are_staged_and_registered(
    tmp_path: pathlib.Path,
) -> None:
    release = HelperPackage.registered_release("1.0")
    assert release is not None
    package = release.clone()
    build = mock.Mock()
    build.get_helpers_root.side_effect = lambda *, relative_to: (
        tmp_path / "absolute-helpers"
        if relative_to == "fsroot"
        else pathlib.Path("relative-helpers")
    )

    tools = package.get_build_tools(build)

    staged = next((tmp_path / "absolute-helpers").iterdir())
    assert staged.read_text(encoding="utf-8").endswith(
        'print("recipe helper")\n'
    )
    if os.name != "nt":
        assert staged.stat().st_mode & 0o111
    assert tools == {"tool": pathlib.Path("relative-helpers") / staged.name}
