from __future__ import annotations

import pathlib
import subprocess
from unittest import mock

import pytest

from ggbuild import dist


def git(directory: pathlib.Path, *arguments: str) -> None:
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        ["git", *arguments],  # ruff: ignore[start-process-with-partial-path]
        cwd=directory,
        check=True,
        stdout=subprocess.DEVNULL,
    )


def test_dirty_tree_digest_tracks_changes_and_untracked_files(
    tmp_path: pathlib.Path,
) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-qm", "initial")

    assert dist.dirty_tree_digest(tmp_path) is None
    tracked.write_text("changed\n", encoding="utf-8")
    changed = dist.dirty_tree_digest(tmp_path)
    assert changed is not None
    (tmp_path / "untracked.txt").write_text("new\n", encoding="utf-8")
    assert dist.dirty_tree_digest(tmp_path) != changed


def test_project_version_key_can_ignore_dirty_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    dist.get_project_version_key.cache_clear()
    with (
        mock.patch.object(dist, "get_dist_version", return_value="1.2.3"),
        mock.patch.object(dist, "get_origin_commit_id", return_value="a" * 40),
        mock.patch.object(dist, "get_origin_source_dir", return_value=tmp_path),
        mock.patch.object(dist, "dirty_tree_digest", return_value="b" * 64),
    ):
        assert dist.get_project_version_key() == (
            "1.2.3.devaaaaaaaaa.dirtybbbbbbbbbbbbbbbb"
        )
        dist.get_project_version_key.cache_clear()
        monkeypatch.setenv("GGBUILD_IGNORE_DIRTY_STATE", "1")
        assert dist.get_project_version_key() == "1.2.3.devaaaaaaaaa"
    dist.get_project_version_key.cache_clear()


def test_ignore_dirty_state_rejects_invalid_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GGBUILD_IGNORE_DIRTY_STATE", "sometimes")
    with pytest.raises(ValueError, match="must be a boolean"):
        dist.ignore_dirty_state()
