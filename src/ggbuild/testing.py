# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Recipe-driven testing of completed build artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import json
import os
import pathlib
import shutil
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import zipfile

import zstandard

from ggbuild.ci_protocol import canonical_json, sha256_file
from ggbuild.execution import detect_host_target, parse_target
from ggbuild.packages import BundledPackage, Test
from ggbuild.planner import load_recipe
from ggbuild.project import ProjectConfig, load_project
from ggbuild.targets.linux.dockerfile import bare_test_image, docker_environment

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


_ARCHIVE_SUFFIXES = (
    ".tar.zstd",
    ".tar.zst",
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
    ".tgz",
    ".zip",
)
_CONTAINER_TMP = str(pathlib.PurePosixPath("/") / "tmp")


def test_environment(test: Test, recipe: BundledPackage) -> dict[str, str]:
    """Create an isolated environment for testing an installed artifact."""
    installation = test.get_build_install_dir(recipe)
    temporary_root = test.get_temp_dir(recipe)
    home = temporary_root / "home"
    home.mkdir(parents=True, exist_ok=True)

    environment = os.environ.copy()
    path = environment.get("PATH")
    environment.update({
        "HOME": str(home),
        "PATH": os.pathsep.join(
            part for part in (str(installation / "bin"), path) if part
        ),
    })
    library_path = str(installation / "lib")
    if test.target.endswith("-apple-darwin"):
        library_variable = "DYLD_LIBRARY_PATH"
    elif "-linux-" in test.target:
        library_variable = "LD_LIBRARY_PATH"
    else:
        library_variable = None
    if library_variable is not None:
        existing_library_path = environment.get(library_variable)
        try:
            test_library_path = str(test.get_test_install_dir(recipe) / "lib")
        except ValueError:
            test_library_path = None
        environment[library_variable] = os.pathsep.join(
            part
            for part in (
                test_library_path,
                library_path,
                existing_library_path,
            )
            if part
        )
    environment.update(recipe.get_test_env(test))
    return environment


def test_is_privileged_root() -> bool:
    """Return whether the test process is privileged host root."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    uid_map = pathlib.Path("/proc/self/uid_map")
    try:
        fields = uid_map.read_text(encoding="utf-8").splitlines()[0].split()
        container_uid, host_uid, count = (int(field) for field in fields)
    except IndexError, OSError, ValueError:
        return True
    return container_uid != 0 or (host_uid == 0 and count == 4294967295)


def metadata_path(archive: pathlib.Path) -> pathlib.Path:
    """Return the generated sibling metadata path for an artifact."""
    for suffix in _ARCHIVE_SUFFIXES:
        if archive.name.endswith(suffix):
            return archive.with_name(
                archive.name.removesuffix(suffix) + ".metadata.json"
            )
    return archive.with_name(archive.name + ".metadata.json")


def load_artifact_metadata(archive: pathlib.Path) -> dict[str, Any]:
    path = metadata_path(archive)
    if not path.is_file():
        raise ValueError(f"artifact metadata is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"artifact metadata is invalid: {path}") from error
    if not isinstance(value, dict):
        raise TypeError("artifact metadata must be an object")
    for field in ("name", "source_version", "target"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"artifact metadata has invalid {field}")
    contents = value.get("contents")
    if isinstance(contents, dict) and archive.name not in contents:
        raise ValueError(
            f"metadata {path.name} does not describe {archive.name}"
        )
    return value


def side_artifacts(
    archive: pathlib.Path, metadata: Mapping[str, Any]
) -> dict[str, pathlib.Path]:
    contents = metadata.get("contents")
    if not isinstance(contents, dict):
        return {}
    result: dict[str, pathlib.Path] = {}
    for filename, raw_details in contents.items():
        if not isinstance(filename, str) or not isinstance(raw_details, dict):
            continue
        role = raw_details.get("artifact_role")
        if not isinstance(role, str):
            continue
        if role in result:
            raise ValueError(f"duplicate artifact side role: {role}")
        path = archive.with_name(filename)
        if not path.is_file():
            raise ValueError(f"artifact side tarball is missing: {path}")
        result[role] = path
    return result


def _test_data_details(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    contents = metadata.get("contents")
    if not isinstance(contents, dict):
        return None
    matches = [
        details
        for details in contents.values()
        if isinstance(details, dict)
        and details.get("artifact_role") == "test-data"
    ]
    if len(matches) > 1:
        raise ValueError("duplicate artifact side role: test-data")
    return matches[0] if matches else None


def _test_data_root(
    installation: pathlib.Path, details: Mapping[str, Any] | None
) -> pathlib.Path | None:
    if details is None:
        return None
    if details.get("overlay") is not True:
        raise ValueError("artifact test data must be an installation overlay")
    raw_root = details.get("root")
    if not isinstance(raw_root, str):
        raise TypeError("artifact test data root is invalid")
    path = pathlib.PurePosixPath(raw_root)
    if path.is_absolute() or raw_root in {"", "."} or ".." in path.parts:
        raise ValueError("artifact test data root is invalid")
    root = (installation / pathlib.Path(*path.parts)).resolve()
    if not root.is_relative_to(installation.resolve()) or not root.is_dir():
        raise ValueError("artifact test data root is missing")
    return root


def _safe_link(path: pathlib.PurePosixPath, target: str) -> bool:
    target_path = pathlib.PurePosixPath(target.replace("\\", "/"))
    windows = pathlib.PureWindowsPath(target)
    if target_path.is_absolute() or windows.drive or windows.root:
        return False
    current = list(path.parent.parts)
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not current:
                return False
            current.pop()
        else:
            current.append(part)
    return True


def _validate_tar_members(members: Sequence[tarfile.TarInfo]) -> None:
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.islnk():
            raise ValueError(f"unsafe archive path: {member.name}")
        if member.issym() and not _safe_link(path, member.linkname):
            raise ValueError(f"unsafe archive symlink: {member.name}")
        if not (
            member.isfile()
            or member.isdir()
            or member.issym()
            or member.type in {tarfile.AREGTYPE, tarfile.REGTYPE}
        ):
            raise ValueError(f"unsupported archive member: {member.name}")


def _extract_tar(archive: pathlib.Path, destination: pathlib.Path) -> None:
    if archive.name.endswith((".tar.zst", ".tar.zstd")):
        with tempfile.TemporaryDirectory() as temporary:
            raw = pathlib.Path(temporary) / "archive.tar"
            with archive.open("rb") as source, raw.open("wb") as target:
                zstandard.ZstdDecompressor().copy_stream(source, target)
            with tarfile.open(raw, mode="r:") as source:
                members = source.getmembers()
                _validate_tar_members(members)
                source.extractall(destination, members=members, filter="data")
        return
    with tarfile.open(archive, mode="r:*") as source:
        members = source.getmembers()
        _validate_tar_members(members)
        source.extractall(destination, members=members, filter="data")


def _extract_zip(archive: pathlib.Path, destination: pathlib.Path) -> None:
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            path = pathlib.PurePosixPath(member.filename)
            mode = member.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
                raise ValueError(f"unsafe archive path: {member.filename}")
        for member in source.infolist():
            source.extract(member, destination)


def extract_artifact(
    archive: pathlib.Path, destination: pathlib.Path
) -> pathlib.Path:
    """Safely extract an artifact and return its installation root."""
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(_ARCHIVE_SUFFIXES[:-1]):
        _extract_tar(archive, destination)
    elif archive.name.endswith(".zip"):
        _extract_zip(archive, destination)
    else:
        raise ValueError(f"unsupported artifact archive: {archive}")
    entries = list(destination.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return destination


def recipe_has_test(recipe: type[BundledPackage]) -> bool:
    return recipe.get_test_script is not BundledPackage.get_test_script


def _resolve_recipe(
    recipe_reference: str, metadata: Mapping[str, Any]
) -> BundledPackage:
    recipe = load_recipe(recipe_reference)
    if not recipe_has_test(recipe):
        raise ValueError(f"root recipe {recipe_reference} has no test hook")
    source_version = str(metadata["source_version"])
    package = recipe.registered_release(source_version)
    if package is None:
        raise ValueError(
            f"root recipe {recipe_reference} has no exact registered "
            f"release {source_version}"
        )
    if str(package.name) != metadata["name"]:
        raise ValueError("artifact metadata does not match the root recipe")
    return package.clone()


def test_result(
    archive: pathlib.Path,
    metadata: Mapping[str, Any],
    recipe: BundledPackage,
) -> dict[str, str]:
    return {
        "artifact_sha256": sha256_file(archive),
        "recipe": f"{type(recipe).__module__}:{type(recipe).__qualname__}",
        "target": str(metadata["target"]),
        "version": str(metadata["source_version"]),
    }


def _write_test_script(recipe: BundledPackage, test: Test) -> pathlib.Path:
    if test_is_privileged_root():
        raise RuntimeError("artifact tests must not run as root")
    script = recipe.get_test_script(test)
    if not script.strip():
        raise RuntimeError("artifact test script is empty")
    work_dir = test.get_temp_dir(recipe)
    script_path = work_dir / "artifact-test.sh"
    header = textwrap.dedent(
        """\
        #!/bin/sh
        set -eu

        """
    )
    script_path.write_text(
        f"{header}{textwrap.dedent(script).rstrip()}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)
    return script_path


def _execute_test_script(recipe: BundledPackage, test: Test) -> None:
    script_path = _write_test_script(recipe, test)
    work_dir = test.get_temp_dir(recipe)
    shell = shutil.which("sh") or shutil.which("bash")
    if shell is None:
        raise RuntimeError("artifact tests require a POSIX shell")
    try:
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            [shell, str(script_path)],
            cwd=work_dir,
            env=test_environment(test, recipe),
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError("artifact test script failed") from error


def _bare_test_environment(
    test: Test, recipe: BundledPackage
) -> dict[str, str]:
    installation = test.get_build_install_dir(recipe)
    temporary_root = test.get_temp_dir(recipe)
    home = temporary_root / "home"
    home.mkdir(parents=True, exist_ok=True)
    try:
        test_install = test.get_test_install_dir(recipe)
    except ValueError:
        test_install = None
    library_paths = [str(installation / "lib")]
    if test_install is not None:
        library_paths.insert(0, str(test_install / "lib"))
    environment = {
        "HOME": str(home),
        "PATH": f"{installation / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        "LD_LIBRARY_PATH": os.pathsep.join(library_paths),
        "TMPDIR": _CONTAINER_TMP,
        "LANG": "C",
        "LC_ALL": "C",
    }
    environment.update(recipe.get_test_env(test))
    return environment


def _execute_bare_linux_test_script(recipe: BundledPackage, test: Test) -> None:
    script_path = _write_test_script(recipe, test)
    root = test.get_work_root().resolve()
    uid = os.getuid() if hasattr(os, "getuid") and os.getuid() else 65534
    gid = os.getgid() if hasattr(os, "getgid") and os.getgid() else 65534
    identity = root / ".container-identity"
    identity.mkdir(parents=True, exist_ok=True)
    passwd = identity / "passwd"
    group = identity / "group"
    home = test.get_temp_dir(recipe) / "home"
    passwd.write_text(
        "root:x:0:0:root:/root:/bin/sh\n"
        f"ggbuild:x:{uid}:{gid}:ggbuild:{home}:/bin/sh\n",
        encoding="utf-8",
    )
    group.write_text(f"root:x:0:\nggbuild:x:{gid}:\n", encoding="utf-8")
    target = str(test.target)
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--read-only",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--platform={docker_environment(target).platform}",
        f"--user={uid}:{gid}",
        "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777",
        f"--volume={root}:{root}:rw",
        f"--volume={passwd}:/etc/passwd:ro",
        f"--volume={group}:/etc/group:ro",
        f"--workdir={script_path.parent}",
    ]
    for name, value in sorted(_bare_test_environment(test, recipe).items()):
        command.append(f"--env={name}={value}")
    command.extend((bare_test_image(target), "/bin/sh", str(script_path)))
    try:
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
            command, check=True
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError("bare Linux artifact test failed") from error


def _create_test(
    *,
    archive: pathlib.Path,
    metadata: Mapping[str, Any],
    recipe: BundledPackage,
    installation: pathlib.Path,
    work_root: pathlib.Path,
    test_install_root: pathlib.Path | None,
) -> Test:
    return Test(
        archive=archive,
        metadata=metadata,
        target=str(metadata["target"]),
        source_version=str(metadata["source_version"]),
        _root_package=recipe.unique_name,
        _installation_root=installation,
        _work_root=work_root,
        _test_install_root=test_install_root,
    )


def _extract_test_data_overlay(
    *,
    archive: pathlib.Path,
    metadata: Mapping[str, Any],
    installation: pathlib.Path,
    extraction_root: pathlib.Path,
) -> pathlib.Path | None:
    details = _test_data_details(metadata)
    test_data_archive = side_artifacts(archive, metadata).get("test-data")
    if test_data_archive is not None:
        extract_artifact(test_data_archive, extraction_root)
    return _test_data_root(installation, details)


def run_test(  # ruff: ignore[too-many-locals]
    archive: pathlib.Path,
    *,
    config: ProjectConfig | None = None,
    recipe_reference: str | None = None,
    work_dir: pathlib.Path | None = None,
    bare_linux: bool = False,
) -> tuple[dict[str, str], pathlib.Path]:
    """Validate, extract, and test one native artifact."""
    archive = archive.resolve()
    if archive.is_dir():
        archive = find_artifact(archive)
    if not archive.is_file():
        raise ValueError(f"artifact does not exist: {archive}")
    metadata = load_artifact_metadata(archive)
    host = detect_host_target()
    artifact_target = parse_target(str(metadata["target"]))
    compatible = (
        host.os == artifact_target.os == "linux"
        and host.arch == artifact_target.arch
        if bare_linux
        else metadata["target"] == host.triple
    )
    if not compatible:
        raise ValueError(
            f"artifact target {metadata['target']} requires an exact native "
            f"host match, got {host.triple}"
        )
    if recipe_reference is None:
        config = config or load_project()
        recipe_reference = config.root_recipe
    recipe = _resolve_recipe(recipe_reference, metadata)
    owned_work = work_dir is None
    root = (
        pathlib.Path(tempfile.mkdtemp(prefix="ggbuild-test-"))
        if owned_work
        else work_dir
    )
    if root is None:
        raise RuntimeError("artifact test work directory is unavailable")
    root.mkdir(parents=True, exist_ok=True)
    try:
        installation = extract_artifact(archive, root / "installation")
        test_install_root = _extract_test_data_overlay(
            archive=archive,
            metadata=metadata,
            installation=installation,
            extraction_root=root / "installation",
        )
        test = _create_test(
            archive=archive,
            metadata=metadata,
            recipe=recipe,
            installation=installation,
            work_root=root,
            test_install_root=test_install_root,
        )
        if bare_linux:
            _execute_bare_linux_test_script(recipe, test)
        else:
            _execute_test_script(recipe, test)
        result = test_result(archive, metadata, recipe)
        result_path = root / "test-result.json"
        result_path.write_text(canonical_json(result), encoding="utf-8")
        if owned_work:
            persistent = archive.with_name(archive.name + ".test-result.json")
            shutil.copy2(result_path, persistent)
            return result, persistent
        return result, result_path
    finally:
        if owned_work:
            shutil.rmtree(root, ignore_errors=True)


def find_artifact(directory: pathlib.Path) -> pathlib.Path:
    """Locate the sole testable archive and its sibling metadata."""
    candidates: list[pathlib.Path] = []
    for metadata_file in sorted(directory.glob("*.metadata.json")):
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        contents = (
            metadata.get("contents") if isinstance(metadata, dict) else None
        )
        if not isinstance(contents, dict):
            continue
        for filename in sorted(contents):
            details = contents[filename]
            if isinstance(details, dict) and details.get("artifact_role"):
                continue
            candidate = directory / filename
            if candidate.is_file() and candidate.name.endswith(
                _ARCHIVE_SUFFIXES
            ):
                candidates.append(candidate)
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise ValueError(
            f"expected one testable artifact in {directory}, "
            f"found {len(unique)}"
        )
    return unique[0]
