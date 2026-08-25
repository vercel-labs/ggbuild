# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import collections
import gzip
import hashlib
import json
import logging
import os.path
import pathlib
import shlex
import shutil
import subprocess
import sys
import tarfile
import textwrap
from compression import zstd

from lograil import (
    ProcessSpec,
    format_progress_line,
    progress,
    run_process_group,
    stage as lifecycle_stage,
)

from ggbuild import packages, targets, tools
from ggbuild.dist import get_project_version_key
from ggbuild.node_cache import policy_for, unmatched_patterns
from ggbuild.targets.generic.output import BuildOutputParser

if TYPE_CHECKING:
    from collections.abc import Callable

    from lograil import ProgressHandle

_supported_compression_schemes = {
    "gzip",
    "zip",
    "zstd",
}
_LOGGER = logging.getLogger("ggbuild.build")
_PACKAGE_STAGE_PROCESSES = {
    "prepare": "preparing",
    "configure": "configuring",
    "build": "building",
    "test_build": "building tests",
    "build_install": "staging",
    "test_install": "staging tests",
    "install": "staging",
}
_COMPRESSION_CHUNK_SIZE = 1024 * 1024


class _BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _BinaryWriter(Protocol):
    def write(self, data: bytes, /) -> int: ...


def compress_file(
    source: pathlib.Path,
    destination: pathlib.Path,
    *,
    encoding: str,
) -> None:
    if encoding not in {"gzip", "zstd"}:
        raise ValueError(f"unsupported compression encoding: {encoding}")
    total = source.stat().st_size
    try:
        with (
            source.open("rb") as source_file,
            progress(
                process="compressing",
                subject=destination.name,
                description=f"reading {source.name}",
                total=total,
            ) as compression_progress,
        ):
            if encoding == "zstd":
                with zstd.open(
                    destination,
                    "wb",
                    level=19,
                ) as destination_file:
                    _copy_compressed(
                        source_file,
                        destination_file,
                        compression_progress,
                    )
            else:
                with gzip.open(
                    destination,
                    "wb",
                    compresslevel=9,
                ) as destination_file:
                    _copy_compressed(
                        source_file,
                        destination_file,
                        compression_progress,
                    )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _copy_compressed(
    source_file: _BinaryReader,
    destination_file: _BinaryWriter,
    compression_progress: ProgressHandle,
) -> None:
    while chunk := source_file.read(_COMPRESSION_CHUNK_SIZE):
        destination_file.write(chunk)
        compression_progress.advance(len(chunk))


class Build(targets.Build):
    _srcroot: pathlib.Path
    _pkgroot: pathlib.Path

    def __init__(
        self,
        target: targets.Target,
        request: targets.BuildRequest,
    ) -> None:
        super().__init__(target, request)
        self._compression = frozenset(request.compression)
        if unsup := self._compression - _supported_compression_schemes:
            raise ValueError(
                f"unsupported compression scheme(s): {', '.join(unsup)}"
            )

    def prepare(self) -> None:
        super().prepare()

        self._pkgroot = self._droot / self._root_pkg.name_slot
        self._srcroot = self._pkgroot / self._root_pkg.name

        self._artifactroot = pathlib.Path("..") / "_artifacts"
        self._buildroot = self._artifactroot / "build"
        self._tmproot = self._artifactroot / "tmp"
        self._installroot = self._artifactroot / "install"
        self._testinstallroot = self._artifactroot / "test-install"

        self._checkpoint_root = self._droot / ".ggbuild-checkpoints"
        self._checkpoint_manifest = self._checkpoint_root / "manifest.json"

    def _checkpoint_path(self, stage: str) -> pathlib.Path:
        return self._checkpoint_root / "stages" / stage

    def _checkpoint_done(self, stage: str) -> bool:
        return self._keepwork and self._checkpoint_path(stage).is_file()

    def _mark_checkpoint(self, stage: str) -> None:
        if not self._keepwork:
            return
        marker = self._checkpoint_path(stage)
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text("complete\n", encoding="utf-8")
        temporary.replace(marker)

    def _run_checkpoint(
        self,
        stage: str,
        action: Callable[[], None],
        *,
        required: pathlib.Path | None = None,
        process: str | None = None,
    ) -> bool:
        if required is not None and not required.exists():
            self._checkpoint_path(stage).unlink(missing_ok=True)
        if self._checkpoint_done(stage):
            _LOGGER.info("Resuming: skipping %s", stage)
            with lifecycle_stage(
                stage,
                process="skip",
                subject=str(self._root_pkg),
            ):
                pass
            return False
        stage_process = process or stage.rsplit("/", maxsplit=1)[-1].replace(
            "-", " "
        )
        with lifecycle_stage(
            stage,
            process=stage_process,
            subject=str(self._root_pkg),
        ):
            action()
        self._mark_checkpoint(stage)
        return True

    def _invalidate_checkpoints(self, *stages: str) -> None:
        if not self._keepwork:
            return
        for stage in stages:
            path = self._checkpoint_path(stage)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    @staticmethod
    def _json_value(value: object) -> object:
        try:
            json.dumps(value)
        except TypeError:
            return repr(value)
        return value

    def _build_manifest(self) -> dict[str, object]:
        def verification_value(verification: object) -> dict[str, object]:
            return {
                "type": type(verification).__qualname__,
                "attributes": {
                    key: self._json_value(value)
                    for key, value in vars(verification).items()
                },
            }

        packages = []
        for package in sorted(self._bundled, key=lambda item: item.unique_name):
            sources = [
                {
                    "url": source.url,
                    "name": source.name,
                    "path": source.path,
                    "extras": self._json_value(source.extras),
                    "verifications": [
                        verification_value(item)
                        for item in source.verifications
                    ],
                }
                for source in package.get_sources()
            ]
            patches = [
                {
                    "package": package_name,
                    "label": label,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                }
                for package_name, patch_set in sorted(
                    package.get_patches().items()
                )
                for label, content in sorted(patch_set)
            ]
            scripts = {
                stage: hashlib.sha256(
                    self.render_package_script(package, stage).encode()
                ).hexdigest()
                for stage in (
                    "prepare",
                    "configure",
                    "build",
                    "test_build",
                    "build_install",
                    "test_install",
                    "install",
                )
            }
            packages.append({
                "name": str(package.name),
                "unique_name": package.unique_name,
                "version": str(package.version),
                "recipe_sha256": self._recipe_digest(package),
                "sources": sources,
                "patches": patches,
                "scripts": scripts,
            })
        return {
            "format": 1,
            "ggbuild_version": get_project_version_key(),
            "target": self.target.triple,
            "root_package": self.root_package.unique_name,
            "revision": self.revision,
            "channel": self.channel,
            "build_source": self._build_source,
            "build_dbgsym": self._build_dbgsym,
            "extra_optimizations": self._extra_opt,
            "compression": sorted(self._compression),
            "dependencies": [
                {
                    "name": str(package.name),
                    "version": str(package.version),
                    "type": type(package).__qualname__,
                }
                for package in sorted(
                    self._build_deps,
                    key=lambda item: (
                        str(item.name),
                        str(item.version),
                        type(item).__qualname__,
                    ),
                )
            ],
            "packages": packages,
        }

    @staticmethod
    def _recipe_digest(package: packages.BasePackage) -> str:
        module = sys.modules[type(package).__module__]
        module_paths = getattr(module, "__path__", None)
        if module_paths is not None:
            roots = [pathlib.Path(path) for path in module_paths]
        else:
            module_file = getattr(module, "__file__", None)
            roots = [pathlib.Path(module_file)] if module_file else []
        digest = hashlib.sha256()
        for root in roots:
            files = sorted(root.rglob("*")) if root.is_dir() else [root]
            for path in files:
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                digest.update(str(path.relative_to(root.parent)).encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _write_json(path: pathlib.Path, value: object) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _package_recipe(package: packages.BasePackage) -> str:
        return f"{type(package).__module__}:{type(package).__qualname__}"

    def _package_cache_policy(
        self, package: packages.BasePackage
    ) -> str | None:
        return policy_for(self._node_cache, (self._package_recipe(package),))

    def _validate_node_cache_names(self) -> None:
        known = {self._package_recipe(package) for package in self._bundled}
        unknown = unmatched_patterns(self._node_cache, known)
        if unknown:
            raise ValueError(
                "--node-cache references nodes outside this build: "
                + ", ".join(unknown)
            )

    @staticmethod
    def _changed_script_stage(
        retained: dict[str, object], current: dict[str, object]
    ) -> str | None:
        old_scripts = retained.get("scripts")
        new_scripts = current.get("scripts")
        if not isinstance(old_scripts, dict) or not isinstance(
            new_scripts, dict
        ):
            return "prepare"
        for stage in (
            "prepare",
            "configure",
            "build",
            "test_build",
            "build_install",
            "test_install",
            "install",
        ):
            if old_scripts.get(stage) != new_scripts.get(stage):
                return stage
        return None

    def _reconcile_checkpoint_manifest(
        self,
        retained: dict[str, object],
        manifest: dict[str, object],
    ) -> set[str]:
        old_packages = retained.get("packages")
        new_packages = manifest.get("packages")
        if not isinstance(old_packages, list) or not isinstance(
            new_packages, list
        ):
            self._invalidate_package_from(self._bundled[0], "prepare")
            return set()
        old_by_name = {
            item.get("unique_name"): item
            for item in old_packages
            if isinstance(item, dict)
            and isinstance(item.get("unique_name"), str)
        }
        new_by_name = {
            item.get("unique_name"): item
            for item in new_packages
            if isinstance(item, dict)
            and isinstance(item.get("unique_name"), str)
        }

        build_fields = {
            "format",
            "target",
            "root_package",
            "revision",
            "channel",
            "build_source",
            "build_dbgsym",
            "extra_optimizations",
            "dependencies",
            "ggbuild_version",
            "ggbuild_sha256",
        }
        global_change = any(
            retained.get(field) != manifest.get(field)
            for field in build_fields
            if field in retained or field in manifest
        )
        raw_retained_stale = retained.get("stale_reused_packages", [])
        retained_stale = {
            name
            for name in (
                raw_retained_stale
                if isinstance(raw_retained_stale, list)
                else []
            )
            if isinstance(name, str)
        }
        stale_reused: set[str] = set()

        for package in self._bundled:
            policy = self._package_cache_policy(package)
            current = new_by_name.get(package.unique_name)
            previous = old_by_name.get(package.unique_name)
            changed = (
                global_change
                or previous != current
                or package.unique_name in retained_stale
            )
            if policy == "reuse":
                if changed:
                    stale_reused.add(package.unique_name)
                continue
            if policy == "ignore":
                self._invalidate_package_from(package, "prepare")
                return set()
            if not changed:
                continue
            if not isinstance(previous, dict) or not isinstance(current, dict):
                self._invalidate_sources()
                return set()
            source_changed = any(
                previous.get(field) != current.get(field)
                for field in ("sources", "patches")
            )
            stage = self._changed_script_stage(previous, current)
            if source_changed or stage == "prepare":
                self._invalidate_sources()
                return set()
            self._invalidate_package_from(package, stage or "configure")
            return set()

        if retained.get("compression") != manifest.get("compression"):
            self._invalidate_checkpoints("package/assemble", "package/export")
        return stale_reused

    def _invalidate_sources(self) -> None:
        if self._srcroot.exists():
            shutil.rmtree(self._srcroot)
        self._invalidate_checkpoints(
            "sources/unpack",
            "sources/patch",
            "packages",
            "package",
        )

    def _validate_checkpoint_manifest(self) -> None:
        if not self._keepwork:
            return
        manifest = self._build_manifest()
        self._checkpoint_root.mkdir(parents=True, exist_ok=True)
        self._validate_node_cache_names()
        if self._checkpoint_manifest.exists():
            loaded = json.loads(
                self._checkpoint_manifest.read_text(encoding="utf-8")
            )
            if not isinstance(loaded, dict):
                raise ValueError(
                    "retained checkpoint manifest is not an object"
                )
            stale_reused = self._reconcile_checkpoint_manifest(loaded, manifest)
            if stale_reused:
                manifest["stale_reused_packages"] = sorted(stale_reused)
        self._write_json(self._checkpoint_manifest, manifest)

    def get_source_abspath(self) -> pathlib.Path:
        return self._srcroot

    def get_path(
        self,
        path: str | pathlib.Path,
        *,
        relative_to: targets.Location,
        package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        """Return *path* relative to *relative_to* location.

        :param pathlike path:
            A path relative to bundle source root.

        :param str relative_to:
            Location name.  Can be one of:
              - ``'sourceroot'``: bundle source root
              - ``'pkgsource'``: package source directory
              - ``'pkgbuild'``: package build directory
              - ``'helpers'``: build helpers directory
              - ``'fsroot'``: filesystem root (makes path absolute)

        :return:
            Path relative to the specified location.
        """

        if relative_to == "sourceroot":
            return pathlib.Path(path)
        if relative_to == "buildroot":
            return pathlib.Path("..") / path
        if relative_to == "pkgsource":
            if package is not None and package.name == self.root_package.name:
                return pathlib.Path("..") / path
            return pathlib.Path("..") / ".." / path
        if relative_to == "pkgbuild":
            return pathlib.Path("..") / ".." / ".." / self._root_pkg.name / path
        if relative_to == "helpers":
            return pathlib.Path("..") / ".." / self._root_pkg.name / path
        if relative_to == "fsroot":
            return (self.get_source_abspath() / path).resolve()
        raise ValueError(f"invalid relative_to argument: {relative_to}")

    def get_helpers_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            self._artifactroot / "helpers", relative_to=relative_to
        )

    def get_source_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path(), relative_to=relative_to)

    def get_tarball_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(self._tmproot / "tarballs", relative_to=relative_to)

    def get_patches_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_tarball_root(relative_to=relative_to)

    def get_extras_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_source_root(relative_to=relative_to) / "extras"

    def get_spec_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path("SPECS"), relative_to=relative_to)

    def get_source_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        if package.name == self.root_package.name:
            return self.get_dir(
                self.root_package.name,
                relative_to=relative_to,
            )
        return self.get_dir(
            pathlib.Path("thirdparty") / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_temp_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._tmproot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_temp_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(self._tmproot, relative_to=relative_to)

    def get_image_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            self._tmproot / "buildroot" / self._root_pkg.name_slot,
            relative_to=relative_to,
        )

    def get_build_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._buildroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_build_install_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        if self._node_staging is not None:
            self._node_staging.mkdir(parents=True, exist_ok=True)
            return self._node_staging
        return self.get_dir(
            self._installroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def get_test_install_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._testinstallroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package or package,
        )

    def _get_tarball_tpl(self, package: packages.BasePackage) -> str:
        rp = self._root_pkg
        version = f"{rp.name}_{rp.version.text}"
        return f"{version}.orig-{package.name}{{part}}.tar{{comp}}"

    def build(self) -> None:
        with lifecycle_stage(
            "prepare/tools", process="prepare", subject="build tools"
        ):
            self.prepare_tools()
        with lifecycle_stage(
            "prepare/checkpoints",
            process="validate",
            subject="checkpoints",
        ):
            self._validate_checkpoint_manifest()
        with lifecycle_stage(
            "prepare/sources",
            process="prepare",
            subject="source archives",
        ):
            self.prepare_tarballs()
        with lifecycle_stage(
            "prepare/patches",
            process="prepare",
            subject="patches",
        ):
            self.prepare_patches()
        unpacked = self._run_checkpoint(
            "sources/unpack",
            self.unpack_sources,
            required=self.get_source_abspath(),
        )
        if unpacked:
            self._invalidate_checkpoints(
                "sources/patch",
                "packages",
                "package",
            )
        patched = self._run_checkpoint(
            "sources/patch",
            self._apply_patches,
            required=self.get_source_abspath(),
        )
        if patched:
            self._invalidate_checkpoints("packages", "package")
        self._write_makefile()
        self._validate_package_checkpoints()
        self._build()

    def package(self) -> None:
        with lifecycle_stage(
            "package/list-files",
            process="packaging",
            subject=str(self._root_pkg),
        ):
            files = self._list_installed_files()
        self._run_checkpoint(
            "package/fixup-binaries",
            lambda: self._fixup_binaries(files),
            required=self.get_image_root(relative_to="fsroot"),
            process="postprocessing",
        )
        files = self._list_installed_files()
        self._root_pkg.validate_install_inventory(self, files)
        self._run_checkpoint(
            "package/assemble",
            lambda: self._package(files),
            required=(
                self.get_intermediate_output_dir(relative_to="fsroot")
                / "build-metadata.json"
            ),
            process="packaging",
        )

    def shrinkwrap(self) -> None:
        expected = self._shipment_paths()
        if any(not path.is_file() for path in expected):
            self._checkpoint_path("package/export").unlink(missing_ok=True)
        self._run_checkpoint(
            "package/export",
            super().shrinkwrap,
            required=self._metadata_shipment_path(),
            process="packaging",
        )

    def _metadata_shipment_path(self) -> pathlib.Path:
        return self._outputroot / f"{self.artifact_fqname}.metadata.json"

    def _shipment_paths(self) -> list[pathlib.Path]:
        intermediates = self.get_intermediate_output_dir(relative_to="fsroot")
        return [
            self._metadata_shipment_path()
            if artifact.name == "build-metadata.json"
            else self._outputroot / artifact.name
            for artifact in intermediates.iterdir()
            if artifact.is_file()
        ]

    def _package_marker(
        self,
        package: packages.BasePackage,
        stage: str,
    ) -> pathlib.Path:
        index = self._bundled.index(package)
        return self._checkpoint_path(
            f"packages/{index:04d}-{package.name}/{stage}"
        )

    def _invalidate_package_from(
        self,
        package: packages.BasePackage,
        stage: str,
    ) -> None:
        stages = (
            "prepare",
            "configure",
            "build",
            "test_build",
            "build_install",
            "test_install",
            "install",
        )
        start = stages.index(stage)
        invalidating = False
        for candidate in self._bundled:
            if candidate is package:
                invalidating = True
            if not invalidating:
                continue
            candidate_start = start if candidate is package else 0
            if candidate_start <= stages.index("configure"):
                build_dir = self.get_build_dir(candidate, relative_to="fsroot")
                if build_dir.exists():
                    shutil.rmtree(build_dir)
            if candidate_start <= stages.index("build_install"):
                install_dir = self.get_build_install_dir(
                    candidate, relative_to="fsroot"
                )
                if install_dir.exists():
                    shutil.rmtree(install_dir)
            if candidate_start <= stages.index("test_install"):
                test_install_dir = self.get_test_install_dir(
                    candidate, relative_to="fsroot"
                )
                if test_install_dir.exists():
                    shutil.rmtree(test_install_dir)
            for candidate_stage in stages[candidate_start:]:
                self._package_marker(candidate, candidate_stage).unlink(
                    missing_ok=True
                )
        image_root = self.get_image_root(relative_to="fsroot")
        if image_root.exists():
            shutil.rmtree(image_root)
        for candidate in self._installable:
            self._package_marker(candidate, "install").unlink(missing_ok=True)
        self._invalidate_checkpoints("package")

    def _validate_package_checkpoints(self) -> None:
        if not self._keepwork:
            return
        image_root = self.get_image_root(relative_to="fsroot")
        for package in self._bundled:
            build_dir = self.get_build_dir(package, relative_to="fsroot")
            install_dir = self.get_build_install_dir(
                package,
                relative_to="fsroot",
            )
            if self._package_marker(package, "configure").exists() and not (
                build_dir.exists()
            ):
                self._invalidate_package_from(package, "configure")
                return
            if self._package_marker(package, "build_install").exists() and not (
                install_dir.exists()
            ):
                self._invalidate_package_from(package, "build_install")
                return

        incomplete_install = any(
            self._package_marker(package, "build_install").exists()
            and not self._package_marker(package, "install").exists()
            for package in self._installable
        )
        missing_image = (
            any(
                self._package_marker(package, "install").exists()
                for package in self._installable
            )
            and not image_root.exists()
        )
        if incomplete_install or missing_image:
            if image_root.exists():
                shutil.rmtree(image_root)
            for package in self._installable:
                install_marker = self._package_marker(package, "install")
                install_marker.unlink(missing_ok=True)
                if not self._package_marker(
                    package,
                    "build_install",
                ).exists():
                    continue
                install_dir = self.get_build_install_dir(
                    package,
                    relative_to="fsroot",
                )
                if not install_marker.exists() and install_dir.exists():
                    shutil.rmtree(install_dir)
                    self._package_marker(
                        package,
                        "build_install",
                    ).unlink(missing_ok=True)
            self._invalidate_checkpoints("package")

    def _apply_patches(self) -> None:
        proot = self.get_patches_root(relative_to="fsroot")
        patch_cmd = shlex.split(self.sh_get_command("patch"))
        dep_root = self.get_dir("thirdparty", relative_to="fsroot")
        my_root = self.get_source_abspath()
        for pkgname, patchname in self._patches:
            sroot = my_root if pkgname == self.root_package.name else dep_root
            patch = proot / patchname
            tools.cmd(
                *([*patch_cmd, "--verbose", "-p1", "-i", str(patch)]),
                hide_stderr=False,
                cwd=sroot,
            )

    def _get_global_env_vars(self) -> dict[str, str]:
        return self.get_sccache_build_env(make=True)

    def _write_makefile(self) -> None:
        image_root = self.get_image_root(relative_to="sourceroot")
        if self._keepwork:
            stamp_root = self._checkpoint_root / "stages" / "packages"
        else:
            stamp_root = self.get_temp_root(relative_to="fsroot") / "stamp"

        previous_build = ""
        rules = []
        install_rules: list[tuple[pathlib.Path, str, str]] = []
        for index, package in enumerate(self._bundled):
            package_key = f"{index:04d}-{package.name}"
            if str(package.name) in self._prebuilt_packages:
                if package in self._installable:
                    install_script = self.render_package_script(
                        package, "install"
                    )
                    if install_script.strip():
                        install_helper = self.sh_write_bash_helper(
                            f"_install_{package.unique_name}.sh",
                            install_script,
                            relative_to="sourceroot",
                            with_interpreter=False,
                        )
                        install_marker = stamp_root / package_key / "install"
                        status_line = format_progress_line(
                            description="install",
                            completed=0,
                            process=_PACKAGE_STAGE_PROCESSES["install"],
                            subject=str(package),
                        )
                        install_rules.append((
                            install_marker,
                            install_helper,
                            status_line,
                        ))
                continue
            for stage in (
                "prepare",
                "configure",
                "build",
                "test_build",
                "build_install",
                "test_install",
            ):
                script = self.render_package_script(package, stage)
                if not script.strip():
                    continue
                helper = self.sh_write_bash_helper(
                    f"_{stage}_{package.unique_name}.sh",
                    script,
                    relative_to="sourceroot",
                    with_interpreter=False,
                )
                marker = stamp_root / package_key / stage
                dependency = f" {previous_build}" if previous_build else ""
                status_line = format_progress_line(
                    description=stage.replace("_", " "),
                    completed=0,
                    process=_PACKAGE_STAGE_PROCESSES[stage],
                    subject=str(package),
                )
                rules.append(
                    textwrap.dedent(f"""\
                    {marker}:{dependency}
                    \t@printf '%s\\n' {shlex.quote(status_line)}
                    \t$(SHELL) {helper}
                    \tmkdir -p "{marker.parent}"
                    \ttouch "{marker}.tmp"
                    \tmv "{marker}.tmp" "{marker}"
                """)
                )
                previous_build = str(marker)

            if package not in self._installable:
                continue
            install_script = self.render_package_script(package, "install")
            if not install_script.strip():
                continue
            install_helper = self.sh_write_bash_helper(
                f"_install_{package.unique_name}.sh",
                install_script,
                relative_to="sourceroot",
                with_interpreter=False,
            )
            install_marker = stamp_root / package_key / "install"
            status_line = format_progress_line(
                description="install",
                completed=0,
                process=_PACKAGE_STAGE_PROCESSES["install"],
                subject=str(package),
            )
            install_rules.append((install_marker, install_helper, status_line))

        previous_install = previous_build
        for marker, install_helper, status_line in install_rules:
            dependency = f" {previous_install}" if previous_install else ""
            rules.append(
                textwrap.dedent(f"""\
                {marker}:{dependency}
                \t@printf '%s\\n' {shlex.quote(status_line)}
                \t$(SHELL) {install_helper}
                \tmkdir -p "{marker.parent}"
                \ttouch "{marker}.tmp"
                \tmv "{marker}.tmp" "{marker}"
            """)
            )
            previous_install = str(marker)

        build_dependency = previous_install
        makefile = textwrap.dedent(
            """\
            .PHONY: build install
            .DEFAULT_GOAL := build

            ROOT = $(dir $(realpath $(firstword $(MAKEFILE_LIST))))

            export SHELL = {bash}

            {env}

            DESTDIR := /

            {rules}

            build: {build_dependency}

            install: build
            \t{copy_tree} -v "{image_root}/" "$(DESTDIR)"

        """
        ).format(
            bash=self.sh_get_command("bash"),
            image_root=image_root,
            rules="\n".join(rules),
            build_dependency=build_dependency,
            copy_tree=self.sh_get_command(
                "copy-tree", relative_to="sourceroot"
            ),
            env="\n".join(
                f"export {var} = {val}"
                for var, val in self._get_global_env_vars().items()
            ),
        )

        pathlib.Path(self._srcroot / "Makefile.ggbuild").write_text(
            makefile, encoding="utf-8"
        )

    def _get_package_install_script(self, pkg: packages.BasePackage) -> str:
        source_root = self.get_source_root(relative_to="pkgbuild")
        install_dir = self.get_build_install_dir(pkg, relative_to="sourceroot")
        image_root = self.get_image_root(relative_to="sourceroot")
        temp_root = self.get_temp_root(relative_to="sourceroot")
        temp_dir = self.get_temp_dir(pkg, relative_to="sourceroot")

        scripts = {
            "install": self._package_helper(
                pkg, "install_list", "gen_install_list"
            ),
            "not_installed": self._package_helper(
                pkg, "no_install_list", "gen_no_install_list"
            ),
            "ignored": self._package_helper(
                pkg, "ignore_list", "gen_ignore_list"
            ),
            "ignored_deps": self._package_helper(
                pkg, "ignored_dependency", "gen_ignored_deps"
            ),
            "extras": self.sh_write_bash_helper(
                f"_install_extras_{pkg.unique_name}.sh",
                self._get_package_extras_script(pkg),
                relative_to="sourceroot",
            ),
        }
        trim_install = self.sh_get_command(
            "trim-install", relative_to="sourceroot"
        )
        copy_tree = self.sh_get_command("copy-tree", relative_to="sourceroot")
        layout = pkg.get_package_layout(self)
        flatten = (
            layout is packages.PackageFileLayout.FLAT
            or layout is packages.PackageFileLayout.SINGLE_BINARY
        )

        node_staging = ""
        keep_staged_files = (
            "--keep-files" if self._node_staging is not None else ""
        )
        if (
            self._node_staging is not None
            and pkg.name == self._root_pkg.name
            and pkg.version == self._root_pkg.version
        ):
            staging = shlex.quote(str(self._node_staging))
            find = self.sh_get_command("find", relative_to="sourceroot")
            node_staging = textwrap.dedent(f"""\
                mkdir -p {staging}
                {find} {staging} -name '*.la' -delete

            """)
        return textwrap.dedent(
            f"""
            pushd "{source_root}" >/dev/null

            {node_staging}

            {scripts["install"]} > "{temp_dir}/install"
            {scripts["not_installed"]} > "{temp_dir}/not-installed"
            {scripts["ignored"]} > "{temp_dir}/ignored"
            {scripts["ignored_deps"]} >> "{temp_root}/ignored-reqs"

            {trim_install} "{temp_dir}/install" \\
                "{temp_dir}/not-installed" "{temp_dir}/ignored" \\
                "{install_dir}" {keep_staged_files} \\
                > "{temp_dir}/install.list"

            {scripts["extras"]} >> "{temp_dir}/install.list"

            {copy_tree} \\
                --verbose \\
                --files-from="{temp_dir}/install.list" \\
                {"--flatten" if flatten else ""} \\
                "{install_dir}/" "{image_root}/"

            popd >/dev/null
        """
        )

    def _package_helper(
        self, pkg: packages.BasePackage, script_name: str, filename: str
    ) -> str:
        return self.sh_write_bash_helper(
            f"_{filename}_{pkg.unique_name}.sh",
            self.render_package_script(pkg, script_name),
            relative_to="sourceroot",
        )

    def _get_package_extras_script(self, pkg: packages.BasePackage) -> str:
        lines = []
        install_dir = self.get_build_install_dir(pkg, relative_to="sourceroot")
        bindir = self.get_bundle_install_path("systembin").relative_to("/")

        lines.append(f'mkdir -p "{install_dir / bindir}"')
        for cmd in pkg.get_exposed_commands(self):
            script, installed = self._get_exposed_command_script(
                pkg,
                cmd,
                install_dir=install_dir,
                bindir=bindir,
            )
            lines.extend((
                script,
                f"printf '%s\\n' {shlex.quote(str(installed))}",
            ))

        extras_dir = self.get_extras_root(relative_to="fsroot")
        for path, content in pkg.get_service_scripts(self).items():
            directory = extras_dir / path.parent.relative_to("/")
            directory.mkdir(parents=True, exist_ok=True)
            with pathlib.Path(directory / path.name).open(
                "w", encoding="utf-8"
            ) as f:
                print(content, file=f)

            lines.append(
                f"printf '%s\\n' {shlex.quote(str(path.relative_to('/')))}"
            )

        return "\n".join(lines)

    def _get_exposed_command_script(
        self,
        pkg: packages.BasePackage,
        cmd: pathlib.Path,
        *,
        install_dir: pathlib.Path,
        bindir: pathlib.Path,
    ) -> tuple[str, pathlib.Path]:
        suffix = self.get_exe_suffix()
        source = cmd.with_name(f"{cmd.name}{suffix}")
        relpath = os.path.relpath(source.relative_to("/"), start=bindir)
        command_name = f"{cmd.name}{pkg.slot_suffix}{suffix}"
        installed = bindir / command_name
        return (
            f'ln -sf "{relpath}" "{install_dir / installed}"',
            installed,
        )

    def _build(self) -> None:
        make = self.sh_get_command("make", relative_to="sourceroot")
        command = shlex.split(make)
        command.extend(["-f", "Makefile.ggbuild"])
        with lifecycle_stage(
            "packages",
            process="building",
            subject=str(self._root_pkg),
        ):
            result = run_process_group([
                ProcessSpec(
                    command,
                    cwd=str(self._srcroot),
                    name="make",
                    process="building",
                    subject=str(self._root_pkg),
                    category="packages",
                    stream="combined",
                    parser=(
                        None
                        if self._io.is_verbose()
                        else BuildOutputParser(
                            work_dir=self._droot,
                            cwd=self._srcroot,
                            source_dirs=tuple(
                                self.get_source_dir(
                                    package,
                                    relative_to="fsroot",
                                )
                                for package in self._bundled
                            ),
                        )
                    ),
                )
            ])
            process_result = result.processes[0]
            if not process_result.success:
                raise subprocess.CalledProcessError(
                    process_result.exit_code,
                    command,
                )

    def _list_installed_files(self) -> list[pathlib.Path]:
        image_root = self.get_image_root(relative_to="fsroot")
        return sorted(
            path.relative_to(image_root)
            for path in image_root.rglob("*")
            if path.is_file() or path.is_symlink()
        )

    def _fixup_rpath(
        self,
        image_root: pathlib.Path,
        binary_relpath: pathlib.Path,
        *,
        additional_rpaths: set[pathlib.Path] | None = None,
    ) -> None:
        pass

    def _strip(
        self, image_root: pathlib.Path, binary_relpath: pathlib.Path
    ) -> None:
        pass

    def _collect_binary_refs(
        self,
        files: list[pathlib.Path],
        image_root: pathlib.Path,
        *,
        additional_rpath_scope: pathlib.Path | None = None,
        additional_rpaths: set[pathlib.Path] | None = None,
    ) -> tuple[
        dict[str, set[pathlib.Path]],
        dict[pathlib.Path, tuple[set[pathlib.Path], set[pathlib.Path]]],
    ]:
        bin_paths: dict[str, set[pathlib.Path]] = collections.defaultdict(set)
        binaries = set()
        refs = {}
        symlinks = []
        for file in files:
            full_path = image_root / file
            inst_path = pathlib.Path("/") / file
            if full_path.is_symlink():
                symlinks.append((inst_path, full_path))
            elif self.target.is_binary_code_file(self, full_path):
                bin_paths[file.name].add(inst_path)
                binaries.add(inst_path)
                original_stat = full_path.stat()
                try:
                    if not self.is_dbgsym_build:
                        self._strip(image_root, file)
                    if self.target.is_dynamically_linked(self, full_path):
                        scoped_rpaths = (
                            additional_rpaths
                            if additional_rpath_scope is not None
                            and file.is_relative_to(additional_rpath_scope)
                            else None
                        )
                        self._fixup_rpath(
                            image_root,
                            file,
                            additional_rpaths=scoped_rpaths,
                        )
                        refs[inst_path] = self.target.get_shlib_refs(
                            self, image_root, file
                        )
                finally:
                    os.utime(
                        full_path,
                        ns=(
                            original_stat.st_atime_ns,
                            original_stat.st_mtime_ns,
                        ),
                    )
        for inst_path, full_path in symlinks:
            target = full_path.readlink()
            target = (
                full_path.parent / target
                if not target.is_absolute()
                else image_root / target.relative_to("/")
            )
            if pathlib.Path("/") / target.relative_to(image_root) in binaries:
                bin_paths[inst_path.name].add(inst_path)
        return bin_paths, refs

    @staticmethod
    def _materialize_shlib(
        image_root: pathlib.Path, resolved_shlib: pathlib.Path
    ) -> tuple[pathlib.Path, pathlib.Path | None]:
        full_path = image_root / resolved_shlib.relative_to("/")
        if not full_path.is_symlink():
            return full_path, None
        real_path = full_path.readlink()
        if not real_path.is_absolute():
            real_path = (full_path.parent / real_path).resolve()
        full_path.unlink()
        shutil.copy2(real_path, full_path)
        return full_path, real_path

    def _check_binary_ref(
        self,
        binary: pathlib.Path,
        referenced_shlib: pathlib.Path,
        rpaths: set[pathlib.Path],
        bin_paths: dict[str, set[pathlib.Path]],
        image_root: pathlib.Path,
    ) -> tuple[pathlib.Path, pathlib.Path | None] | None:
        if self.target.is_allowed_system_shlib(self, referenced_shlib):
            return None
        bundled = bin_paths.get(referenced_shlib.name, set())
        for rpath in rpaths:
            resolved_shlib = (rpath / referenced_shlib.name).resolve()
            if resolved_shlib in bundled:
                return self._materialize_shlib(image_root, resolved_shlib)
        rpath_list = ":".join(map(str, rpaths))
        if rpath_list:
            raise AssertionError(
                f"{binary} links to {referenced_shlib}, which is neither an "
                "allowed system library, nor a bundled library in rpath: "
                f"{rpath_list}"
            )
        raise AssertionError(
            f"{binary} links to {referenced_shlib} which is not an allowed "
            f"system library, and {binary} does not define a library rpath"
        )

    @staticmethod
    def _remove_so_symlinks(path: pathlib.Path) -> None:
        for sibling in path.parent.iterdir():
            if not sibling.is_symlink():
                continue
            target = sibling.readlink()
            if not target.is_absolute():
                target = (sibling.parent / target).resolve()
            if target == path:
                sibling.unlink()

    def _fixup_binaries(
        self,
        files: list[pathlib.Path],
        *,
        image_root: pathlib.Path | None = None,
        additional_rpath_scope: pathlib.Path | None = None,
        additional_rpaths: set[pathlib.Path] | None = None,
    ) -> None:
        # Here we examine all produced executables for references to
        # shared libraries outside of what's bundled in the package and
        # what's allowed to be linked to on the target system (typically,
        # just the C library).
        if image_root is None:
            image_root = self.get_image_root(relative_to="fsroot")
        bin_paths, refs = self._collect_binary_refs(
            files,
            image_root,
            additional_rpath_scope=additional_rpath_scope,
            additional_rpaths=additional_rpaths,
        )
        to_remove: set[pathlib.Path] = set()
        used_shlibs: set[pathlib.Path] = set()
        for binary, (shlibs, rpaths) in refs.items():
            for referenced_shlib in shlibs:
                result = self._check_binary_ref(
                    binary, referenced_shlib, rpaths, bin_paths, image_root
                )
                if result is not None:
                    used_path, remove_path = result
                    used_shlibs.add(used_path)
                    if remove_path is not None:
                        to_remove.add(remove_path)
        for path in to_remove - used_shlibs:
            self._remove_so_symlinks(path)
            path.unlink()
        for path in used_shlibs:
            self._remove_so_symlinks(path)

    def _package(self, files: list[pathlib.Path]) -> None:
        pkg = self._root_pkg
        image_root = self.get_image_root(relative_to="fsroot")
        archive_name = self.artifact_fqname
        archives_abs = self.get_intermediate_output_dir(relative_to="fsroot")
        layout = pkg.get_package_layout(self)
        dbgsym_root = self._dbgsym_artifact_root()
        with lifecycle_stage(
            "package/archive",
            process="packaging",
            subject=archive_name,
        ):
            if layout is packages.PackageFileLayout.SINGLE_BINARY:
                installrefs, installrefs_ct = self._package_single_binary(
                    files, archive_name, image_root, archives_abs
                )
            else:
                installrefs, installrefs_ct = self._package_tree(
                    archive_name, image_root, archives_abs, layout
                )
            side_refs, side_contents = self._package_side_artifacts(
                archive_name, archives_abs, dbgsym_root=dbgsym_root
            )
            installrefs.extend(side_refs)
            installrefs_ct.update(side_contents)

        with pathlib.Path(archives_abs / "build-metadata.json").open(
            "w", encoding="utf-8"
        ) as vf:
            json.dump(
                {
                    "installrefs": installrefs,
                    "contents": installrefs_ct,
                    "repository": "generic",
                    **self._root_pkg.get_artifact_metadata(self),
                },
                vf,
            )

    def _package_side_artifacts(
        self,
        archive_name: str,
        archives: pathlib.Path,
        *,
        dbgsym_root: pathlib.Path | None,
    ) -> tuple[list[str], dict[str, dict[str, object]]]:
        archives.mkdir(parents=True, exist_ok=True)
        side_artifacts: dict[str, pathlib.Path] = {}
        test_data_root = self._test_data_root()
        if test_data_root is not None:
            side_artifacts["test-data"] = test_data_root
        if dbgsym_root is not None:
            if "dbgsym" in side_artifacts:
                raise ValueError("artifact side role 'dbgsym' is reserved")
            side_artifacts["dbgsym"] = dbgsym_root
        refs: list[str] = []
        contents: dict[str, dict[str, object]] = {}
        for role, source in sorted(side_artifacts.items()):
            if not source.exists():
                raise RuntimeError(
                    f"artifact side data for {role} does not exist: {source}"
                )
            tarball = archives / f"{archive_name}.{role}.tar"
            with tarfile.open(tarball, mode="w") as archive:
                arcname = (
                    pathlib.Path(archive_name) / ".ggbuild-test-data"
                    if role == "test-data"
                    else pathlib.Path(role)
                )
                archive.add(source, arcname=arcname)
            ref = f"{tarball.name}.zst"
            compress_file(tarball, archives / ref, encoding="zstd")
            tarball.unlink()
            refs.append(ref)
            contents[ref] = {
                "artifact_role": role,
                "encoding": "zstd",
                "suffix": ".tar.zst",
                "type": "application/x-tar",
            }
            if role == "test-data":
                contents[ref]["overlay"] = True
                contents[ref]["root"] = ".ggbuild-test-data"
        return refs, contents

    def _test_data_root(self) -> pathlib.Path | None:
        script = self.render_package_script(
            self._root_pkg,
            "test_install_list",
            # This script is run with the test staging root as its cwd, not
            # from the package source root used by ordinary build stages.
            # Keep render_package_script's build-directory wrapper absolute.
            relative_to="fsroot",
        )
        if not script.strip():
            return None

        source_root = self.get_test_install_dir(
            self._root_pkg, relative_to="fsroot"
        )
        test_root = self.get_temp_root(relative_to="fsroot") / "test-data"
        file_list = (
            self.get_temp_dir(self._root_pkg, relative_to="fsroot")
            / "test.list"
        )
        if test_root.exists():
            shutil.rmtree(test_root)
        file_list.write_text(
            tools.cmd(
                *shlex.split(self.sh_get_command("bash")),
                "-c",
                script,
                cwd=source_root,
            )
            + "\n",
            encoding="utf-8",
        )
        tools.cmd(
            *shlex.split(
                self.sh_get_command("copy-tree", relative_to="fsroot")
            ),
            "--verbose",
            f"--files-from={file_list}",
            f"{source_root}/",
            test_root,
        )
        return self._postprocess_test_data_root(test_root)

    def _postprocess_test_data_root(
        self, staged_root: pathlib.Path
    ) -> pathlib.Path:
        """Relocate test data through the ordinary artifact image pipeline."""
        temporary = self.get_temp_root(relative_to="fsroot")
        image_root = self.get_image_root(relative_to="fsroot")
        test_image = temporary / "test-data-image"
        if test_image.exists():
            shutil.rmtree(test_image)
        shutil.copytree(image_root, test_image, symlinks=True)

        prefix = self.get_bundle_install_prefix().relative_to("/")
        test_root = test_image / prefix / ".ggbuild-test-data"
        test_root.parent.mkdir(parents=True, exist_ok=True)
        staged_root.replace(test_root)
        files = sorted(
            path.relative_to(test_image)
            for path in test_image.rglob("*")
            if path.is_file() or path.is_symlink()
        )
        test_scope = prefix / ".ggbuild-test-data"
        self._fixup_binaries(
            files,
            image_root=test_image,
            additional_rpath_scope=test_scope,
            additional_rpaths={
                pathlib.Path("/") / test_scope / "lib",
            },
        )
        return test_root

    def _dbgsym_artifact_root(self) -> pathlib.Path | None:
        if not self.is_dbgsym_build:
            return None
        image_root = self.get_image_root(relative_to="fsroot")
        dbgsym_root = self.get_temp_root(relative_to="fsroot") / "dbgsym"
        dbgsym_root.mkdir(parents=True, exist_ok=True)
        for path in sorted(image_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if not self.target.is_binary_code_file(self, path):
                continue
            relative = path.relative_to(image_root)
            destination = dbgsym_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._extract_dbgsym(path, destination)
        return dbgsym_root

    def _extract_dbgsym(
        self, binary: pathlib.Path, destination: pathlib.Path
    ) -> None:
        if self.target.triple.endswith("-apple-darwin"):
            tools.cmd(
                "dsymutil", binary, "-o", pathlib.Path(f"{destination}.dSYM")
            )
            tools.cmd("strip", "-S", binary)
            return
        tools.cmd("objcopy", "--only-keep-debug", binary, destination)
        tools.cmd("strip", binary)
        tools.cmd(
            "objcopy",
            f"--add-gnu-debuglink={destination}",
            binary,
        )

    def _package_single_binary(
        self,
        files: list[pathlib.Path],
        archive_name: str,
        image_root: pathlib.Path,
        archives: pathlib.Path,
    ) -> tuple[list[str], dict[str, dict[str, object]]]:
        if len(files) != 1:
            raise AssertionError(
                "Single-file package build produced multiple files!"
            )
        filename = files[0]
        base_name = f"{archive_name}{filename.suffix}"
        tools.cmd(
            "cp",
            image_root / filename,
            self.get_intermediate_output_dir() / base_name,
            cwd=self.get_source_abspath(),
        )
        mime = self.target.executable_mime_type
        refs = [base_name]
        contents: dict[str, dict[str, object]] = {
            base_name: {
                "type": mime,
                "encoding": "identity",
                "suffix": filename.suffix,
            }
        }
        commands = {
            "zip": (
                "zip",
                ["-9", f"{base_name}.zip"],
                ".zip",
                "identity",
                ".zip",
            ),
        }
        for compression in self._compression:
            if compression in {"gzip", "zstd"}:
                extension = ".gz" if compression == "gzip" else ".zst"
                compress_file(
                    archives / base_name,
                    archives / f"{base_name}{extension}",
                    encoding=compression,
                )
                ref = f"{base_name}{extension}"
                refs.append(ref)
                contents[ref] = {
                    "type": mime,
                    "encoding": compression,
                    "suffix": extension,
                }
                continue
            command, args, extension, encoding, suffix = commands[compression]
            tools.cmd(command, *args, base_name, cwd=archives)
            ref = f"{base_name}{extension}"
            refs.append(ref)
            contents[ref] = {
                "type": "application/zip" if compression == "zip" else mime,
                "encoding": encoding,
                "suffix": suffix,
            }
        return refs, contents

    def _package_tree(
        self,
        archive_name: str,
        image_root: pathlib.Path,
        archives: pathlib.Path,
        layout: packages.PackageFileLayout,
    ) -> tuple[list[str], dict[str, dict[str, object]]]:
        refs: list[str] = []
        contents: dict[str, dict[str, object]] = {}
        prefix = self.get_bundle_install_prefix().relative_to("/")
        if self._compression & {"gzip", "zstd"}:
            source = (
                image_root
                if layout is packages.PackageFileLayout.FLAT
                else image_root / prefix
            )
            tarball = f"{archive_name}.tar"
            with tarfile.open(archives / tarball, mode="w") as archive:
                archive.add(source, arcname=archive_name)
            for compression, extension in (
                ("zstd", ".zst"),
                ("gzip", ".gz"),
            ):
                if compression in self._compression:
                    compress_file(
                        archives / tarball,
                        archives / f"{tarball}{extension}",
                        encoding=compression,
                    )
                    ref = f"{tarball}{extension}"
                    refs.append(ref)
                    contents[ref] = {
                        "type": "application/x-tar",
                        "encoding": compression,
                        "suffix": f".tar{extension}",
                    }
            (archives / tarball).unlink(missing_ok=True)
        if "zip" in self._compression:
            source_dir = (
                "."
                if layout is packages.PackageFileLayout.FLAT
                else archive_name
            )
            if source_dir != ".":
                (image_root / archive_name).symlink_to(
                    prefix, target_is_directory=True
                )
            tools.cmd(
                "zip",
                "-9",
                "-r",
                archives / f"{archive_name}.zip",
                source_dir,
                cwd=image_root,
            )
            ref = f"{archive_name}.zip"
            refs.append(ref)
            contents[ref] = {
                "type": "application/zip",
                "encoding": "identity",
                "suffix": ".zip",
            }
        return refs, contents
