from __future__ import annotations

import pathlib

import pytest
from cleo.io.null_io import NullIO
from poetry.vcs.git import backend as poetry_git

import ggbuild.app  # ruff: ignore[unused-import]  # Initialize import graph.
from ggbuild import patches, tools
from ggbuild.packages import sources
from ggbuild.patches import PatchError, parse_patch, select_patch_variants
from tests.patch_fixture import PatchPackage


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        (
            "17.10",
            [
                ("major", "major 17 patch\n"),
                ("point", "point 17.10 patch\n"),
            ],
        ),
        ("18.4", []),
    ],
)
def test_legacy_patch_version_suffixes(
    version: str, expected: list[tuple[str, str]]
) -> None:
    package = PatchPackage.registered_release(version)

    assert package is not None
    assert package.get_patches().get("patch-package", []) == expected


def test_explicit_range_patch_name() -> None:
    patch = parse_patch(pathlib.Path("package__fix__17-17.5.patch"))

    assert patch.name == "fix"
    assert patch.matches("17.4")
    assert not patch.matches("17.5")


def test_exact_variant_overrides_legacy_major(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broad = tmp_path / "patch-package__fix-17.patch"
    exact = tmp_path / "patch-package__fix__17.10.patch"
    broad.write_text("broad", encoding="utf-8")
    exact.write_text("exact", encoding="utf-8")
    monkeypatch.setattr(
        "ggbuild.patches.patch_variants",
        lambda _recipe: (parse_patch(broad), parse_patch(exact)),
    )
    package = PatchPackage.registered_release("17.10")

    assert package is not None
    assert select_patch_variants(package) == (parse_patch(exact),)


def test_incomparable_matching_ranges_are_ambiguous(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "patch-package__fix__17-18.patch"
    second = tmp_path / "patch-package__fix__-17.11.patch"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    monkeypatch.setattr(
        "ggbuild.patches.patch_variants",
        lambda _recipe: (parse_patch(first), parse_patch(second)),
    )
    package = PatchPackage.registered_release("17.10")

    assert package is not None
    with pytest.raises(PatchError, match="ambiguous patches"):
        select_patch_variants(package)


def _commit(repo: tools.git.Git, message: str) -> None:
    repo.run("add", "--all")
    repo.run(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--quiet",
        "-m",
        message,
    )


def _reroll_fixture(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str,
) -> patches.RerollResult:
    canonical_path = tmp_path / "canonical"
    canonical = tools.git.Git.initialize(canonical_path)
    tracked = canonical_path / "tracked.txt"
    tracked.write_text("a\nx\ny\nb\nc\n", encoding="utf-8")
    _commit(canonical, "base")
    base = canonical.head
    canonical.run("tag", "17.10")

    tracked.write_text("a\nx\ny\nB\nc\n", encoding="utf-8")
    _commit(canonical, "change b")
    patch_path = tmp_path / "patch-package__change-17.patch"
    patch_path.write_text(
        canonical.run(
            "format-patch", "--stdout", "-1", "HEAD", strip_output=False
        ),
        encoding="utf-8",
    )

    canonical.run("checkout", "--quiet", "--detach", base)
    tracked.write_text("A\nx\ny\nb\nc\n", encoding="utf-8")
    _commit(canonical, "change a")
    canonical.run("tag", "17.11")

    source_path = tmp_path / "source"
    source_path.mkdir()
    source_text = "a\nx\ny\nb\nc\n" if version == "17.10" else "A\nx\ny\nb\nc\n"
    (source_path / "tracked.txt").write_text(source_text, encoding="utf-8")

    monkeypatch.setattr(PatchPackage, "canonical_repo", str(canonical_path))
    monkeypatch.setattr(
        patches,
        "patch_variants",
        lambda _recipe: (parse_patch(patch_path),),
    )
    monkeypatch.setattr(
        poetry_git.Git,
        "get_default_source_root",
        lambda: tmp_path / "cache",
    )
    package = PatchPackage(
        version,
        resolved_sources=[sources.LocalSource(str(source_path), "source")],
    )
    base_source = tmp_path / "base-source"
    base_source.mkdir()
    (base_source / "tracked.txt").write_text(
        "a\nx\ny\nb\nc\n", encoding="utf-8"
    )
    base_package = PatchPackage(
        "17.10",
        resolved_sources=[sources.LocalSource(str(base_source), "base-source")],
    )
    return patches.reroll_patches(
        package, NullIO(), base_packages=(base_package,)
    )


def test_reroll_retains_cleanly_applicable_patch(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _reroll_fixture(tmp_path, monkeypatch, version="17.10")

    assert len(result.patches) == 1
    assert result.patches[0].retained
    assert result.writes == {}


def test_reroll_retains_patch_with_acceptable_fuzz(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _reroll_fixture(tmp_path, monkeypatch, version="17.11")

    assert len(result.patches) == 1
    destination = result.patches[0].destination
    assert destination is None
    assert result.writes == {}
