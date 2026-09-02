# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Self,
    TypeIs,
    TypeVar,
    cast,
    overload,
    override,
)

import collections
import copy
import dataclasses
import enum
import functools
import hashlib
import inspect
import os
import pathlib
import platform
import pprint
import re
import shlex
import shutil
import sys
import textwrap
from collections.abc import Iterable, Mapping, Sequence

import packaging.utils
from poetry.core.constraints import version as poetry_version
from poetry.core.packages import (
    dependency as poetry_dep,
    dependency_group as poetry_depgroup,
    package as poetry_pkg,
)
from poetry.core.spdx import helpers as poetry_spdx_helpers
from poetry.core.version import pep440 as poetry_pep440
from poetry.core.version.pep440 import segments as poetry_pep440_segments
from poetry.repositories import exceptions as poetry_repo_exc

from ggbuild import tools

from . import repository, sources as af_sources

if TYPE_CHECKING:
    from cleo.io import io as cleo_io
    from poetry.repositories import repository as poetry_repo

    from ggbuild import targets


get_build_requirements = repository.get_build_requirements
set_build_requirements = repository.set_build_requirements
canonicalize_name = packaging.utils.canonicalize_name
NormalizedName = packaging.utils.NormalizedName
all_requires_include_build_reqs: bool = False


type Args = dict[str, str | pathlib.Path | None]


@dataclasses.dataclass(frozen=True, slots=True)
class Test:
    """Prepared paths and metadata supplied to test script generators."""

    archive: pathlib.Path
    metadata: Mapping[str, Any]
    target: str
    source_version: str
    _root_package: str
    _installation_root: pathlib.Path
    _work_root: pathlib.Path
    _test_install_root: pathlib.Path | None = None

    def get_build_install_dir(self, package: BasePackage) -> pathlib.Path:
        if package.unique_name != self._root_package:
            raise ValueError(
                "only the root package has a shipped build-install directory"
            )
        return self._installation_root

    def get_test_install_dir(self, package: BasePackage) -> pathlib.Path:
        if package.unique_name != self._root_package:
            raise ValueError(
                "only the root package has a shipped test-install directory"
            )
        if self._test_install_root is None:
            raise ValueError("artifact test sidecar is missing")
        return self._test_install_root

    def get_temp_dir(self, package: BasePackage) -> pathlib.Path:
        name = pathlib.PurePath(package.unique_name)
        if len(name.parts) != 1 or name.name in {"", ".", ".."}:
            raise ValueError(f"invalid package identity: {package.unique_name}")
        path = self._work_root / "packages" / name.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_work_root(self) -> pathlib.Path:
        """Return the isolated root shared with the test runtime."""
        return self._work_root


class AliasPackage(poetry_pkg.Package):
    def __repr__(self) -> str:
        return f"<AliasPackage {self.unique_name}>"


class PackageFileLayout(enum.IntEnum):
    REGULAR = enum.auto()
    FLAT = enum.auto()
    SINGLE_BINARY = enum.auto()


@dataclasses.dataclass
class MetaPackage:
    base_name: str
    """Package name not including the slot"""

    name: str
    """Package name with slot (if any)"""

    description: str
    dependencies: dict[str, str]


class PackageWithPrettyVersion(poetry_pkg.Package):
    @override
    def __init__(
        self,
        name: str,
        version: str | poetry_version.Version,
        source_type: str | None = None,
        source_url: str | None = None,
        source_reference: str | None = None,
        source_resolved_reference: str | None = None,
        source_subdirectory: str | None = None,
        features: Iterable[str] | None = None,
        develop: bool = False,
        yanked: str | bool = False,
        *,
        pretty_version: str | None = None,
    ) -> None:
        super().__init__(
            name=name,
            version=version,
            source_type=source_type,
            source_url=source_url,
            source_reference=source_reference,
            source_resolved_reference=source_resolved_reference,
            source_subdirectory=source_subdirectory,
            features=features,
            develop=develop,
            yanked=yanked,
        )

        if pretty_version:
            self._pretty_version = pretty_version
        elif isinstance(version, poetry_version.Version):
            self._pretty_version = version.text
        else:
            self._pretty_version = version

    @property
    def pretty_version(self) -> str:
        return self._pretty_version


class _BasePackageCore(PackageWithPrettyVersion):
    @property
    def slot_suffix(self) -> str:
        return ""

    def get_dep_pkg_name(self) -> str:
        """Name used by pkg-config or CMake to refer to this package."""
        return str(self.name).upper()

    def get_dep_pkg_config_script(self) -> str | None:
        return None

    @property
    def provides_pkg_config(self) -> bool:
        return False

    @property
    def provides_shlibs(self) -> bool:
        return False

    @property
    def provides_c_headers(self) -> bool:
        return False

    @property
    def provides_build_tools(self) -> bool:
        return False

    def get_pkg_config_meta(self) -> PkgConfigMeta:
        return PkgConfigMeta(
            pkg_name=self.get_dep_pkg_name(),
            pkg_config_script=self.get_dep_pkg_config_script(),
            provides_pkg_config=self.provides_pkg_config,
            provides_shlibs=self.provides_shlibs,
            provides_c_headers=self.provides_c_headers,
            provides_build_tools=self.provides_build_tools,
        )

    def get_sources(self) -> list[af_sources.BaseSource]:
        raise NotImplementedError

    def get_requirements(self) -> list[poetry_dep.Dependency]:
        return []

    def get_build_requirements(self) -> list[poetry_dep.Dependency]:
        return []

    def get_license_files_patterns(self) -> list[str]:
        return [
            "LICENSE*",
            "LICENCE*",
            "COPYING",
            "NOTICE",
            "COPYRIGHT",
        ]

    def get_prepare_script(self, build: targets.Build) -> str:
        return ""

    def get_configure_script(self, build: targets.Build) -> str:
        return ""

    def get_build_script(self, build: targets.Build) -> str:
        raise NotImplementedError(f"{self}.build()")

    def get_build_env(self, build: targets.Build, wd: str) -> Args:
        package = cast("BasePackage", self)
        all_build_deps = build.get_build_reqs(package, recursive=True)
        cache_env = {
            key: f"!{value}"
            for key, value in build.get_sccache_build_env().items()
        }
        return cache_env | build.get_ld_env(all_build_deps, wd=wd)

    def get_build_install_script(self, build: targets.Build) -> str:
        script = ""

        licenses = self.get_license_files_patterns()
        if licenses:
            package = cast("BasePackage", self)
            sdir = build.get_source_dir(package, relative_to="pkgbuild")
            legaldir = build.get_install_path(package, "legal").relative_to("/")
            lic_dest = (
                build.get_build_install_dir(package, relative_to="pkgbuild")
                / legaldir
            )
            prefix = str(lic_dest / self.name)
            licenses_pattern = "{" + ",".join(licenses) + "}"
            script += textwrap.dedent(
                f"""\
                mkdir -p "{lic_dest}"
                for _lic_src in "{sdir}"/{licenses_pattern}; do
                    if [ -f "$_lic_src" ]; then
                        cp "$_lic_src" "{prefix}-$(basename "$_lic_src")"
                    elif [ -d "$_lic_src" ]; then
                        for _lic_file in "$_lic_src"/*; do
                            _lic_dest="{prefix}-$(basename "$_lic_src")"
                            _lic_name="$(basename "$_lic_file")"
                            cp "$_lic_file" "$_lic_dest-$_lic_name"
                        done
                    fi
                done
                """
            )

        return script

    def get_test_build_script(self, build: targets.Build) -> str:
        """Build test harnesses after the production build."""
        return ""

    def get_test_install_script(self, build: targets.Build) -> str:
        """Install prebuilt test harnesses into the test staging root."""
        return ""


class _BasePackageBuild(  # ruff: ignore[too-many-public-methods]
    _BasePackageCore
):
    def get_build_install_env(self, build: targets.Build, wd: str) -> Args:
        return self.get_build_env(build, wd=wd)

    def get_test_build_env(self, build: targets.Build, wd: str) -> Args:
        return self.get_build_env(build, wd=wd)

    def get_test_install_env(self, build: targets.Build, wd: str) -> Args:
        return self.get_test_build_env(build, wd=wd)

    def get_build_tools(self, build: targets.Build) -> dict[str, pathlib.Path]:
        module = sys.modules[type(self).__module__]
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            raise RuntimeError(f"module {module.__name__} has no source file")
        helper_source = pathlib.Path(module_file).parent / "helpers"
        if not helper_source.is_dir():
            return {}

        helper_root = build.get_helpers_root(relative_to="fsroot")
        helper_root.mkdir(parents=True, exist_ok=True)
        helper_relative = build.get_helpers_root(relative_to="sourceroot")
        result: dict[str, pathlib.Path] = {}
        for source in sorted(helper_source.iterdir()):
            if not source.is_file():
                continue
            destination = helper_root / f"{self.unique_name}-{source.name}"
            if (
                not destination.exists()
                or destination.read_bytes() != source.read_bytes()
            ):
                shutil.copy2(source, destination)
            destination.chmod(0o755)
            result[source.stem] = helper_relative / destination.name
        return result

    def get_build_tools_path(
        self,
        build: targets.Build,
        *,
        relative_to: targets.Location = "pkgbuild",
        relative_to_package: BasePackage | None = None,
        wd: str | None = None,
    ) -> str | None:
        if wd is None:
            wd = "$(pwd -P)"

        package = cast("BasePackage", self)
        rel_bin_path = package.get_install_path(build, "bin")
        if rel_bin_path:
            root_path = build.get_build_install_dir(
                package,
                relative_to=relative_to,
                relative_to_package=relative_to_package,
            )
            bin_path = root_path / rel_bin_path.relative_to("/")
            return f"{wd}/{shlex.quote(str(bin_path))}"
        return None

    def get_patches(
        self,
    ) -> dict[str, list[tuple[str, str]]]:
        return {}

    def _get_file_list_script(
        self,
        build: targets.Build,
        listname: str,
        *,
        entries: list[str],
        imply_parents: bool,
    ) -> str:
        if entries:
            package = cast("BasePackage", self)
            script = package.write_file_list_script(
                build, listname, entries, imply_parents=imply_parents
            )
        else:
            script = ""

        return script

    def get_file_install_entries(self, build: targets.Build) -> list[str]:
        entries: list[str] = []
        package = cast("BasePackage", self)
        for shlib in package.get_shlibs(build):
            entries.extend(
                f"{{libdir}}/{shlib_pat}"
                for shlib_pat in build.target.get_shlib_filename_patterns(shlib)
            )
        entries.extend(
            f"{{legaldir}}/{{name}}-{lic_pattern}"
            for lic_pattern in self.get_license_files_patterns()
        )
        return entries

    def get_install_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_install_entries(build)
        entries += [
            str(p.relative_to("/")) for p in self.get_service_scripts(build)
        ]
        return self._get_file_list_script(
            build,
            "install",
            entries=entries,
            imply_parents=True,
        )

    def get_file_test_install_entries(self, build: targets.Build) -> list[str]:
        return []

    def get_test_install_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_test_install_entries(build)
        if not entries:
            return ""
        package = cast("BasePackage", self)
        return package.write_file_list_script(
            build,
            "test-install",
            entries,
            imply_parents=True,
            root=build.get_test_install_dir(package, relative_to="fsroot"),
        )

    def get_file_no_install_entries(self, build: targets.Build) -> list[str]:
        return [
            # Never install static libraries or libtool stuff
            "{libdir}/*.a",
            "{libdir}/*.la",
        ]

    def get_no_install_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_no_install_entries(build)
        return self._get_file_list_script(
            build,
            "no_install",
            entries=entries,
            imply_parents=False,
        )

    def get_file_ignore_entries(self, build: targets.Build) -> list[str]:
        return [
            # ignore binaries by default, packages can opt-in with
            # explicit entries in install.list
            "{bindir}/*",
            # autoconf, pkg-config and CMake stuff are not useful
            "{datadir}/aclocal/**",
            "{libdir}/aclocal/**",
            "{datadir}/cmake/**",
            "{libdir}/cmake/**",
            "{datadir}/pkgconfig/**",
            "{libdir}/pkgconfig/**",
            # likewise, include files
            "{includedir}/**",
            # And the documentation
            "{mandir}/**",
            "{docdir}/**",
            "{infodir}/**",
            "{datadir}/gtk-doc/**",
            "{bundlemandir}/**",
            "{bundledocdir}/**",
            "{bundleinfodir}/**",
            "{bundledatadir}/gtk-doc",
            "{bundledatadir}/gtk-doc/**",
        ]

    def get_ignore_list_script(self, build: targets.Build) -> str:
        entries = self.get_file_ignore_entries(build)
        return self._get_file_list_script(
            build,
            "ignore",
            entries=entries,
            imply_parents=True,
        )

    def get_private_libraries(self, build: targets.Build) -> list[str]:
        return []

    def get_extra_system_requirements(
        self, build: targets.Build
    ) -> dict[str, list[str]]:
        return {}

    def get_before_install_script(self, build: targets.Build) -> str:
        return ""

    def get_after_install_script(self, build: targets.Build) -> str:
        return ""

    def get_service_scripts(
        self, build: targets.Build
    ) -> dict[pathlib.Path, str]:
        return {}

    def get_bin_shims(self, build: targets.Build) -> dict[str, str]:
        return {}

    def get_exposed_commands(self, build: targets.Build) -> list[pathlib.Path]:
        return []


class BasePackage(_BasePackageBuild):
    def get_install_path(
        self,
        build: targets.Build,
        aspect: targets.InstallAspect,
    ) -> pathlib.Path | None:
        pkg_config = self.get_pkg_config_meta()
        unavailable = {
            "lib": not pkg_config.provides_shlibs,
            "include": not pkg_config.provides_c_headers,
            "bin": not (
                pkg_config.provides_build_tools or pkg_config.pkg_config_script
            ),
        }
        if unavailable.get(aspect):
            return None
        return build.get_install_path(self, aspect)

    def get_shlibs(self, build: targets.Build) -> list[str]:
        return []

    def get_dep_commands(self) -> list[str]:
        return []

    def get_dep_install_subdir(
        self,
        build: targets.Build,
        pkg: BasePackage,
    ) -> pathlib.Path:
        return pathlib.Path()

    def get_root_install_subdir(
        self,
        build: targets.Build,
    ) -> pathlib.Path:
        raise NotImplementedError

    def write_file_list_script(
        self,
        build: targets.Build,
        listname: str,
        entries: list[str],
        *,
        imply_parents: bool,
        root: pathlib.Path | None = None,
        path_variables: Mapping[str, str | pathlib.Path] | None = None,
    ) -> str:
        if root is None:
            root = build.get_build_install_dir(self, relative_to="pkgbuild")

        paths: dict[str, str | pathlib.Path] = {}
        for aspect in (
            "systembin",
            "sysconf",
            "bin",
            "data",
            "include",
            "lib",
            "legal",
            "doc",
            "man",
            "info",
        ):
            path = build.get_install_path(self, aspect)
            paths[f"{aspect}dir"] = path.relative_to("/")
            path = build.get_bundle_install_path(aspect)
            paths[f"bundle{aspect}dir"] = path.relative_to("/")

        paths["name"] = self.name
        paths["version"] = str(self.version)
        paths["prefix"] = build.get_rel_install_prefix(self)
        paths["exesuffix"] = build.get_exe_suffix()
        if path_variables is not None:
            paths.update(path_variables)

        processed_entries = [
            entry.strip().format(**paths).replace("/", os.sep)
            for entry in entries
        ]

        pyscript = textwrap.dedent(
            """\
            import glob
            import pathlib

            tmp = pathlib.Path({root!r})

            patterns = {patterns}

            matches = set()

            for pattern in patterns:
                if pattern.endswith('/**'):
                    pattern += "/*"
                for p in tmp.glob(pattern):
                    if p.exists():
                        rel_p = p.relative_to(tmp)
                        matches.add(rel_p)
                        if {imply_parents}:
                            matches.update(rel_p.parents)

            for match in matches:
                print(match)
        """
        ).format(
            root=str(root),
            patterns=pprint.pformat(processed_entries),
            imply_parents=str(imply_parents),
        )

        scriptfile_name = f"_gen_{listname}_list_{self.unique_name}.py"

        return build.sh_write_python_helper(
            scriptfile_name, pyscript, relative_to="pkgbuild"
        )

    def get_package_layout(self, build: targets.Build) -> PackageFileLayout:
        return PackageFileLayout.REGULAR

    @property
    def all_requires(
        self,
    ) -> list[poetry_dep.Dependency]:
        if all_requires_include_build_reqs:
            return super().all_requires + get_build_requirements(self)
        return super().all_requires


@dataclasses.dataclass(kw_only=True)
class PkgConfigMeta:
    #: Name used by pkg-config or CMake or autoconf to refer to this package.
    pkg_name: str
    #: Package-provided a package-config script, eg bin/package-config
    pkg_config_script: str | None = None
    #: Whether the package provides a pkg-config (*.pc) file
    provides_pkg_config: bool = False
    #: Whether the package provides shared libraries
    provides_shlibs: bool = False
    #: Whether the package provides C/C++ header files
    provides_c_headers: bool = False
    #: Whether the package provides build tools
    provides_build_tools: bool = False


BundledPackage_T = TypeVar("BundledPackage_T", bound="BundledPackage")

type RequirementList = list[str | poetry_dep.Dependency]
type VersionedRequirements = Mapping[
    str | poetry_version.VersionConstraint,
    Sequence[str | poetry_dep.Dependency],
]
type RequirementsSpec = RequirementList | VersionedRequirements


def _is_versioned_requirements(
    spec: RequirementsSpec,
) -> TypeIs[VersionedRequirements]:
    return isinstance(spec, Mapping)


class _BundledPackageResolution(BasePackage):
    ident: ClassVar[str]
    title: ClassVar[str | None] = None
    aliases: ClassVar[list[str] | None] = None
    description: str = ""
    license_id: ClassVar[str | None] = None
    group: ClassVar[str]
    url: ClassVar[str | None] = None
    identifier: ClassVar[str]

    source_version: str

    artifact_requirements: ClassVar[RequirementsSpec] = []
    artifact_build_requirements: ClassVar[RequirementsSpec] = []
    build_requires: list[poetry_dep.Dependency]

    options: dict[str, Any]
    metadata_tags: dict[str, str]

    sources: ClassVar[list[str | af_sources.SourceDecl]]
    resolved_sources: list[af_sources.BaseSource]
    sha256: str | None
    canonical_repo: ClassVar[str | None] = None

    @property
    def slot(self) -> str:
        return ""

    @property
    def slot_suffix(self) -> str:
        if self.slot:
            return f"-{self.slot}"
        return ""

    @property
    def supports_out_of_tree_builds(self) -> bool:
        return True

    @property
    def name_slot(self) -> str:
        return f"{self.name}{self.slot_suffix}"

    @property
    def name_for_user_and_dir(self) -> str:
        return self.name

    def version_includes_revision(self) -> bool:
        return True

    def version_includes_slot(self) -> bool:
        return True

    @classmethod
    def get_source_url_variables(cls, version: str) -> dict[str, str]:
        return {}

    @classmethod
    def _source_from_decl(
        cls, source: af_sources.SourceDecl, version: str
    ) -> af_sources.BaseSource:
        underscore_version = version.replace(".", "_")
        dash_version = version.replace(".", "-")
        clsfile = inspect.getsourcefile(cls)
        url = source["url"].format(
            version=version,
            underscore_version=underscore_version,
            dash_version=dash_version,
            major_v=version.split(".", maxsplit=1)[0],
            major_minor_v=".".join(version.split(".")[:2]),
            dirname=pathlib.Path(clsfile).parent if clsfile else None,
            **cls.get_source_url_variables(version),
        )
        source_extras = source.get("extras")
        extras: af_sources.SourceExtraDecl = (
            {**source_extras} if source_extras else {}
        )
        extras.setdefault("version", version)
        extras.setdefault("vcs_version", cls.to_vcs_version(extras["version"]))
        mirrors = source.get("mirrors", [])
        if mirrors:
            extras["mirrors"] = [
                mirror.format(
                    version=version,
                    underscore_version=underscore_version,
                    dash_version=dash_version,
                    major_v=version.split(".", maxsplit=1)[0],
                    major_minor_v=".".join(version.split(".")[:2]),
                    dirname=pathlib.Path(clsfile).parent if clsfile else None,
                    **cls.get_source_url_variables(version),
                )
                for mirror in mirrors
            ]
        result = af_sources.source_for_url(url, extras)
        result.path = cast("str", source.get("path"))
        if checksum_algorithm := source.get("csum_algo"):
            checksum_url = source.get("csum_url")
            if checksum_url:
                checksum_url = checksum_url.format(
                    version=version,
                    underscore_version=underscore_version,
                    dash_version=dash_version,
                )
            result.add_verification(
                af_sources.HashVerification(
                    checksum_algorithm,
                    hash_url=checksum_url,
                    hash_value=source.get("csum"),
                )
            )
        return result

    @classmethod
    def _get_sources(cls, version: str | None) -> list[af_sources.BaseSource]:
        resolved_version = version or "HEAD"
        return [
            af_sources.source_for_url(source)
            if isinstance(source, str)
            else cls._source_from_decl(source, resolved_version)
            for source in cls.sources
        ]

    @classmethod
    def to_vcs_version(cls, version: str) -> str:
        return version

    @classmethod
    def canonical_ref(cls, source_version: str) -> str:
        return cls.to_vcs_version(source_version)

    @classmethod
    def parse_vcs_version(cls, version: str) -> poetry_version.Version:
        return poetry_version.Version.parse(version)

    @classmethod
    def get_package_repository(
        cls, target: targets.Target, io: cleo_io.IO
    ) -> poetry_repo.Repository:
        return repository.bundle_repo

    @classmethod
    def version_from_source(
        cls,
        source_dir: pathlib.Path,
    ) -> str:
        raise NotImplementedError

    @classmethod
    def get_vcs_source(
        cls, io: cleo_io.IO, ref: str | None = None
    ) -> af_sources.GitSource | None:
        sources = cls._get_sources(version=ref)
        if isinstance(sources[0], af_sources.GitSource):
            return sources[0]
        return None

    @classmethod
    def resolve_vcs_repo(
        cls,
        io: cleo_io.IO,
        version: str | None = None,
    ) -> tools.git.GitClone:
        source = cls.get_vcs_source(io, ref=version)
        if source is None:
            raise ValueError("Unable to resolve non-git bundled package")
        return source.download(io)

    @classmethod
    def get_next_feature_version(
        cls,
        version: poetry_version.Version,
    ) -> poetry_version.Version:
        return version.next_minor()

    @classmethod
    def version_from_vcs_version(
        cls,
        io: cleo_io.IO,
        repo: tools.git.Git,
        vcs_version: str,
        *,
        is_release: bool,
    ) -> str:
        ver = repo.run("describe", "--tags", vcs_version).strip()
        ver = ver.removeprefix("v")

        parts = ver.rsplit("-", maxsplit=2)
        if (
            len(parts) == 3
            and parts[2].startswith("g")
            and parts[1].isdigit()
            and parts[1].isascii()
        ):
            # Have commits after the tag
            parsed_ver = cls.get_next_feature_version(
                cls.parse_vcs_version(parts[0]),
            )

            if not is_release:
                commits = repo.run(
                    "rev-list",
                    "--count",
                    vcs_version,
                )

                ver = parsed_ver.replace(
                    local=None,
                    pre=None,
                    dev=poetry_pep440.ReleaseTag("dev", int(commits)),
                ).to_string()
            else:
                ver = parsed_ver.to_string()

        return ver

    @classmethod
    def _resolve_source_version(
        cls,
        io: cleo_io.IO,
        version: str | None,
        *,
        is_release: bool,
    ) -> tuple[list[af_sources.BaseSource], str, str, str, bool]:
        sources = list(cls._get_sources(version))
        vcs_source = cls.get_vcs_source(io, version)
        if vcs_source is not None:
            sources[0] = vcs_source
            repo = cls.resolve_vcs_repo(io, version)
            vcs_version = cls.to_vcs_version(version) if version else "HEAD"
            source_version = repo.resolve_local_rev(vcs_version)
            if source_version is None:
                raise ValueError(f"could not resolve {vcs_version}")
            resolved_version = cls.version_from_vcs_version(
                io, repo, source_version, is_release=is_release
            )
            git_date = repo.run(
                "log",
                "-1",
                "--format=%cd",
                "--date=format-local:%Y%m%d%H",
                source_version,
                env={**os.environ, "TZ": "UTC", "LANG": "C"},
            )
            return sources, resolved_version, source_version, git_date, True
        if version is not None:
            return sources, version, version, "", False
        if isinstance(sources[0], af_sources.LocalSource):
            source_version = cls.version_from_source(
                pathlib.Path(sources[0].url)
            )
            return sources, source_version, source_version, "", False
        raise ValueError("version must be specified for non-git packages")

    @classmethod
    def resolve(
        cls,
        io: cleo_io.IO,
        *,
        name: NormalizedName | None = None,
        version: str | None = None,
        revision: str | None = None,
        is_release: bool = False,
        target: targets.Target,
        requires: list[poetry_dep.Dependency] | None = None,
    ) -> Self:
        sources, version, source_version, git_date, is_git = (
            cls._resolve_source_version(io, version, is_release=is_release)
        )
        revision = revision or "1"

        if is_git:
            ver = cls.parse_vcs_version(version)
        else:
            ver = poetry_version.Version.parse(version)

        local = ver.local
        if isinstance(ver.local, tuple):
            local = ver.local
        elif ver.local is None:
            local = ()
        else:
            local = (ver.local,)

        if is_git:
            ver = ver.replace(
                local=(
                    *local,
                    f"r{revision}",
                    f"d{git_date}",
                    f"g{source_version[:9]}",
                )
            )
        else:
            ver = ver.replace(local=(*local, f"r{revision}"))

        package_cls = cast("type[BundledPackage]", cls)
        version, pretty_version = package_cls.format_version(ver)

        return cast(
            "Self",
            package_cls(
                version=version,
                pretty_version=pretty_version,
                source_version=source_version,
                resolved_sources=sources,
                requires=requires,
                name=name,
            ),
        )


# @lat: [[recipes#Recipes and Sources#Registered Releases]]
class _BundledPackageInstance(_BundledPackageResolution):
    @classmethod
    def format_version(cls, ver: poetry_version.Version) -> tuple[str, str]:
        full_ver = pep440_to_semver(ver)
        version_base = pep440_to_semver(ver.without_local())
        version_hash = hashlib.sha256(full_ver.encode("utf-8")).hexdigest()
        version = f"{version_base}+{version_hash[:7]}"
        pretty_version = f"{full_ver}.s{version_hash[:7]}"
        return version, pretty_version

    def get_root_install_subdir(self, build: targets.Build) -> pathlib.Path:
        return pathlib.Path(self.name_slot)

    def get_dep_pkg_config_meta(self, dep: BasePackage) -> PkgConfigMeta:
        if isinstance(dep, BundledPackage):
            return dep.get_pkg_config_meta()
        return _get_bundled_pkg_config_meta(dep.name)

    def get_sources(self) -> list[af_sources.BaseSource]:
        if self.resolved_sources:
            return self.resolved_sources
        return self._get_sources(version=self.source_version)

    @classmethod
    def _verified_release_sources(
        cls, version: str, sha256: str
    ) -> list[af_sources.BaseSource]:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("sha256 must be a lowercase hexadecimal digest")
        release_sources = cls._get_sources(version)
        if len(release_sources) != 1 or not isinstance(
            release_sources[0], af_sources.HttpsSource
        ):
            raise ValueError(
                "sha256 requires exactly one archive source; "
                "use resolved_sources for unusual source layouts"
            )
        release_sources[0].add_verification(
            af_sources.HashVerification("sha256", hash_value=sha256)
        )
        return release_sources

    @classmethod
    def registered_release(cls, version: str) -> Self | None:
        for package in repository.bundle_repo.packages:
            if type(package) is cls and package.source_version == version:
                if not isinstance(package, cls):
                    raise TypeError(
                        f"registered package {package!r} has an invalid type"
                    )
                return package
        return None

    def get_patches(
        self,
    ) -> dict[str, list[tuple[str, str]]]:
        from ggbuild import patches  # ruff: ignore[import-outside-top-level]

        return patches.patches_for_build(cast("BundledPackage", self))

    def __init__(
        self,
        version: str | poetry_version.Version,
        pretty_version: str | None = None,
        *,
        sha256: str | None = None,
        source_version: str | None = None,
        requires: list[poetry_dep.Dependency] | None = None,
        options: Mapping[str, Any] | None = None,
        resolved_sources: list[af_sources.BaseSource] | None = None,
        name: NormalizedName | None = None,
    ) -> None:
        if self.title is None:
            raise RuntimeError(
                f"{type(self)!r} does not define the required title attribute"
            )

        if name is None:
            name = canonicalize_name(self.__class__.ident)

        super().__init__(name, version, pretty_version=pretty_version)

        reqs = list(requires) if requires is not None else []

        reqs.extend(self.get_requirements())

        if reqs:
            if not self.has_dependency_group(poetry_depgroup.MAIN_GROUP):
                self.add_dependency_group(
                    poetry_depgroup.DependencyGroup(poetry_depgroup.MAIN_GROUP),
                )

            main_group = self._dependency_groups[poetry_depgroup.MAIN_GROUP]
            for req in reqs:
                main_group.add_dependency(req)

        resolved_sources = (
            self._verified_release_sources(str(version), sha256)
            if sha256 is not None
            else resolved_sources
        )

        if resolved_sources is not None:
            self.resolved_sources = list(resolved_sources)
        else:
            self.resolved_sources = []

        self.metadata_tags = {}

        repository.set_build_requirements(self, self.get_build_requirements())
        self.description = type(self).description
        license_id = type(self).license_id
        if license_id is not None:
            self.license = poetry_spdx_helpers.license_by_id(license_id)
        self.options = dict(options) if options is not None else {}
        self.sha256 = sha256
        if source_version is None:
            self.source_version = self.pretty_version
        else:
            self.source_version = source_version

        repository.bundle_repo.add_package(self)

        if self.aliases:
            for alias in self.aliases:
                pkg = AliasPackage(name=alias, version=self.version)
                pkg.add_dependency(
                    poetry_dep.Dependency(self.name, self.version)
                )
                repository.bundle_repo.add_package(pkg)

    @property
    def pretty_version(self) -> str:
        return self._pretty_version

    def _get_requirements(
        self,
        spec: RequirementsSpec,
        prop: str,
    ) -> list[poetry_dep.Dependency]:
        reqs = []

        req_spec: list[str | poetry_dep.Dependency] = []

        if _is_versioned_requirements(spec):
            for version_spec, ver_reqs in spec.items():
                constraint = (
                    poetry_version.parse_constraint(version_spec)
                    if isinstance(version_spec, str)
                    else version_spec
                )
                if constraint.allows(self.version):
                    req_spec.extend(ver_reqs)

            if not req_spec and spec:
                raise RuntimeError(
                    f"{prop} for {self.name!r} are not "
                    f"empty, but don't match the requested version "
                    f"{self.version}"
                )
        else:
            req_spec = list(spec)

        for item in req_spec:
            if isinstance(item, str):
                reqs.append(poetry_dep.Dependency.create_from_pep_508(item))
            else:
                reqs.append(item)
        return reqs

    def get_requirements(self) -> list[poetry_dep.Dependency]:
        return self._get_requirements(
            self.artifact_requirements,
            "artifact_requirements",
        )

    def get_build_requirements(self) -> list[poetry_dep.Dependency]:
        return self._get_requirements(
            self.artifact_build_requirements,
            "artifact_build_requirements",
        )

    def clone(self) -> Self:
        return copy.deepcopy(self)

    def is_root(self) -> bool:
        return False


class _BundledPackageFiles(_BundledPackageInstance):
    @overload
    def read_support_files(
        self, build: targets.Build, file_glob: str, *, binary: Literal[False]
    ) -> dict[str, str]: ...

    @overload
    def read_support_files(
        self, build: targets.Build, file_glob: str
    ) -> dict[str, str]: ...

    @overload
    def read_support_files(
        self, build: targets.Build, file_glob: str, *, binary: Literal[True]
    ) -> dict[str, bytes]: ...

    def read_support_files(
        self, build: targets.Build, file_glob: str, *, binary: bool = False
    ) -> dict[str, str] | dict[str, bytes]:
        mod = sys.modules[type(self).__module__]
        mod_file = mod.__file__
        if mod_file is None:
            raise RuntimeError(f"module {mod.__name__} has no source file")
        path = pathlib.Path(mod_file).parent / file_glob

        result = {}

        for support_path in path.parent.glob(path.name):
            mode = "rb" if binary else "r"
            with support_path.open(mode) as f:
                content = f.read()
                name = support_path.name
                if not binary and name.endswith(".in"):
                    package = cast("BundledPackage", self)
                    content = build.format_package_template(content, package)
                    name = name[:-3]
                    name = name.replace("SLOT", self.slot)
                    name = name.replace(
                        "IDENTIFIER",
                        build.target.get_package_system_ident(build, package),
                    )
                result[name] = content

        return result

    def _read_install_entries(
        self,
        build: targets.Build,
        listname: str,
    ) -> list[str]:
        mod = sys.modules[type(self).__module__]
        mod_file = mod.__file__
        if mod_file is None:
            raise RuntimeError(f"module {mod.__name__} has no source file")
        path = pathlib.Path(mod_file).parent / f"{listname}.list"

        entries: list[str] = []

        if path.exists():
            with pathlib.Path(path).open(encoding="utf-8") as f:
                entries.extend(f)

        return entries

    def get_file_install_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_install_entries(build)
        return entries + self._read_install_entries(build, "install")

    def get_file_no_install_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_no_install_entries(build)
        return entries + self._read_install_entries(build, "no_install")

    def get_file_ignore_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_ignore_entries(build)
        return entries + self._read_install_entries(build, "ignore")

    def get_file_test_install_entries(self, build: targets.Build) -> list[str]:
        entries = super().get_file_test_install_entries(build)
        return entries + self._read_install_entries(build, "test.install")

    def get_prepare_script(self, build: targets.Build) -> str:
        script = ""

        if not self.supports_out_of_tree_builds:
            sdir = shlex.quote(
                str(build.get_source_dir(self, relative_to="pkgbuild"))
            )
            script += f"test ./ -ef {sdir} || cp -a {sdir}/* ./\n"

        return script

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str = "",
    ) -> str:
        wd = "${_wd}"
        env = build.sh_format_command(
            "env",
            self.get_build_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )
        cmd = build.sh_format_args(args, force_args_eq=True)

        if target:
            if target.startswith("!"):
                target = target[1:]
            else:
                target = shlex.quote(target)

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {cmd} {target}
            """
        )

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        service_scripts = self.get_service_scripts(build)
        if service_scripts:
            install = build.sh_get_command("cp", relative_to="pkgbuild")
            extras_dir = build.get_extras_root(relative_to="pkgbuild")
            install_dir = build.get_build_install_dir(
                self, relative_to="pkgbuild"
            )
            ensuredir = build.target.get_action("ensuredir", build)
            if TYPE_CHECKING:
                assert isinstance(ensuredir, targets.EnsureDirAction)

            commands = []

            for service_path in service_scripts:
                path = service_path.relative_to("/")
                commands.append(
                    ensuredir.get_script(path=str((install_dir / path).parent))
                )
                args: dict[str, str | None] = {
                    str(extras_dir / path): None,
                    str(install_dir / path): None,
                }
                cmd = build.sh_format_command(install, args)
                commands.append(cmd)

            return script + "\n" + "\n".join(commands)

        return script


# @lat: [[recipes#Recipes and Sources]]
class BundledPackage(_BundledPackageFiles):
    def get_test_env(self, test: Test) -> Mapping[str, str]:
        """Return recipe-specific environment variables for artifact tests."""
        return {}

    def get_test_script(self, test: Test) -> str:
        """Return the artifact test script; recipes opt in by overriding it."""
        return ""

    def validate_install_inventory(
        self, build: targets.Build, files: Sequence[pathlib.Path]
    ) -> None:
        """Reject unexpected files after production trimming."""

    def get_pre_start_script(self, build: targets.Build) -> str:
        return ""

    def get_resources(self, build: targets.Build) -> dict[str, bytes]:
        return self.read_support_files(build, "resources/*", binary=True)

    def get_service_scripts(
        self, build: targets.Build
    ) -> dict[pathlib.Path, str]:
        return build.target.service_scripts_for_package(build, self)

    def get_bin_shims(self, build: targets.Build) -> dict[str, str]:
        return self.read_support_files(build, "shims/*")

    def __repr__(self) -> str:
        return f"<BundledPackage {self.unique_name}>"

    def get_meta_packages(
        self,
        build: targets.Build,
        root_version: str,
    ) -> list[MetaPackage]:
        return []

    def get_conflict_packages(
        self,
        build: targets.Build,
        root_version: str,
    ) -> list[str]:
        return []

    def get_transition_packages(
        self,
        build: targets.Build,
    ) -> list[str]:
        return []

    def get_provided_packages(
        self,
        build: targets.Build,
        root_version: str,
    ) -> list[tuple[str, str]]:
        return []

    def get_version_details(self) -> dict[str, Any]:
        pv = poetry_version.Version.parse(self.pretty_version)

        prerelease = []
        if pv.pre is not None:
            prerelease.append({
                "phase": semver_pre_tag(pv),
                "number": pv.pre.number,
            })

        if pv.dev is not None:
            prerelease.append({
                "phase": pv.dev.phase,
                "number": pv.dev.number,
            })

        if pv.local:
            local: tuple[str | int, ...]
            if isinstance(pv.local, tuple):
                local = pv.local
            elif pv.local is None:
                local = ()
            else:
                local = (pv.local,)

            ver_metadata = self.parse_version_metadata(local)
        else:
            ver_metadata = {}

        return {
            "major": pv.major,
            "minor": pv.minor,
            "patch": pv.patch,
            "prerelease": prerelease,
            "metadata": ver_metadata,
        }

    def get_artifact_metadata(self, build: targets.Build) -> dict[str, Any]:
        metadata = {
            "name": self.name,
            "source_version": self.source_version,
            "version": pep440_to_semver(self.version),
            "version_details": self.get_version_details(),
            "revision": build.revision,
            "build_date": build.build_date.isoformat(),
            "target": build.target.triple,
            "architecture": build.target.machine_architecture,
            "dist": build.target.ident,
            "channel": build.channel,
            "tags": self.metadata_tags,
        }

        if self.slot:
            metadata["version_slot"] = self.slot

        return metadata

    def parse_version_metadata(
        self,
        segments: tuple[str | int, ...],
    ) -> dict[str, str]:
        result = {}
        pfx_map = self.get_version_metadata_fields()
        for segment in segments:
            segment_str = str(segment)
            for pfx_len in (1, 2):
                key = pfx_map.get(segment_str[:pfx_len])
                if key is not None:
                    result[key] = segment_str[pfx_len:]
                    break
            else:
                raise RuntimeError(
                    f"unrecognized version metadata field `{segment}`"
                )

        return result

    def get_version_metadata_fields(self) -> dict[str, str]:
        return {
            "r": "build_revision",
            "d": "source_date",
            "g": "scm_revision",
            "t": "target",
            "s": "build_hash",
            "b": "build_type",
        }

    def set_metadata_tags(self, tags: Mapping[str, str]) -> None:
        self.metadata_tags = dict(tags)


def merge_requirements(
    *specs: RequirementsSpec,
) -> RequirementsSpec:
    result: collections.defaultdict[
        str | poetry_version.VersionConstraint,
        list[str | poetry_dep.Dependency],
    ] = collections.defaultdict(list)
    for spec in specs:
        if _is_versioned_requirements(spec):
            for k, v in spec.items():
                result[k].extend(v)
        else:
            result["*"].extend(spec)
    return dict(result)


@functools.cache
def _get_bundled_pkg_config_meta(name: str) -> PkgConfigMeta:
    package = _get_pkg_in_bundle_repo(poetry_dep.Dependency(name, "*"))
    if isinstance(package, BasePackage):
        return package.get_pkg_config_meta()
    return PkgConfigMeta(
        pkg_name=package.name.upper(),
        pkg_config_script=None,
        provides_pkg_config=False,
        provides_shlibs=False,
        provides_c_headers=False,
        provides_build_tools=False,
    )


@functools.cache
def _get_pkg_in_bundle_repo(dep: poetry_dep.Dependency) -> poetry_pkg.Package:
    packages = repository.bundle_repo.find_packages(dep)
    if not packages:
        raise poetry_repo_exc.PackageNotFoundError(
            f"package {dep.pretty_name} not found in bundled repo."
        )

    packages.sort(key=lambda pkg: pkg.version, reverse=True)
    return packages[0]


def get_bundled_pkg(dep: poetry_dep.Dependency) -> BundledPackage:
    package = _get_pkg_in_bundle_repo(dep)
    if not isinstance(package, BundledPackage):
        raise TypeError(
            f"package {package} is in the bundle repo, "
            "but it is not a BundledPackage"
        )
    return package


class PrePackagedPackage(BundledPackage):
    pass


# @lat: [[recipes#Recipes and Sources#Script-Oriented Build Hooks]]
class BuildSystemMakePackage(BundledPackage):
    def get_build_script(self, build: targets.Build) -> str:
        args = self.get_make_args(build)
        target = self.get_make_target(build)
        return self.get_build_command(build, args, target)

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str | Sequence[str] = "",
    ) -> str:
        wd = "${_wd}"
        # Undefining MAKELEVEL is required because
        # some package makefiles have
        # conditions on MAKELEVEL.
        env = build.sh_format_command(
            "env",
            {"-uMAKELEVEL": None} | self.get_build_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )
        make_args = {f"-j{build.build_parallelism}": None} | args
        make = build.sh_get_command("make", args=make_args, force_args_eq=True)

        if isinstance(target, str):
            target = shlex.quote(target) if target else ""
        else:
            target = shlex.join(target)

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {make} {target}
            """
        )

    def get_build_install_command(
        self,
        build: targets.Build,
        args: Args,
        target: str,
    ) -> str:
        wd = "${_wd}"
        env = build.sh_format_command(
            "env",
            {"-uMAKELEVEL": None} | self.get_build_install_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )
        make_args: Args = {f"-j{build.build_parallelism}": None}
        build.sh_append_quoted_flags(
            make_args,
            "DESTDIR",
            [self.sh_get_make_install_destdir(build, wd=wd)],
        )
        make_args |= args
        make = build.sh_get_command("make", args=make_args, force_args_eq=True)

        if target:
            target = shlex.quote(target)

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {make} {target}
            """
        )

    def get_test_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str | Sequence[str] = "",
    ) -> str:
        return self._get_test_make_command(build, args, target, install=False)

    def get_test_install_command(
        self,
        build: targets.Build,
        args: Args,
        target: str | Sequence[str] = "",
    ) -> str:
        return self._get_test_make_command(build, args, target, install=True)

    def _get_test_make_command(
        self,
        build: targets.Build,
        args: Args,
        target: str | Sequence[str],
        *,
        install: bool,
    ) -> str:
        wd = "${_wd}"
        env_args = (
            self.get_test_install_env(build, wd=wd)
            if install
            else self.get_test_build_env(build, wd=wd)
        )
        env = build.sh_format_command(
            "env",
            {"-uMAKELEVEL": None} | env_args,
            force_args_eq=True,
            linebreaks=False,
        )
        make_args = {f"-j{build.build_parallelism}": None} | args
        make = build.sh_get_command("make", args=make_args, force_args_eq=True)
        rendered_target = (
            shlex.quote(target)
            if isinstance(target, str)
            else shlex.join(target)
        )
        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {make} {rendered_target}
            """
        )

    def get_make_args(self, build: targets.Build) -> Args:
        return {}

    def get_make_target(self, build: targets.Build) -> str:
        return ""

    def get_make_install_args(self, build: targets.Build) -> Args:
        return {}

    def get_make_install_target(self, build: targets.Build) -> str:
        return "install"

    def sh_get_make_install_destdir(
        self,
        build: targets.Build,
        wd: str,
    ) -> str:
        instdir = build.get_build_install_dir(
            self, relative_to="pkgbuild"
        ) / self.get_make_install_destdir_subdir(build)
        if instdir.is_absolute():
            return shlex.quote(str(instdir))
        return f"{wd}/{shlex.quote(str(instdir))}"

    def get_make_install_destdir_subdir(
        self,
        build: targets.Build,
    ) -> pathlib.Path:
        return pathlib.Path()

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        if target := self.get_make_install_target(build):
            args = self.get_make_install_args(build)
            make_install = self.get_build_install_command(build, args, target)
            script += "\n" + make_install

        return script

    def get_binary_output_dir(self) -> pathlib.Path:
        """Return the result-binary path relative to the build directory."""
        return pathlib.Path("bin")


class BundledCPackage(BuildSystemMakePackage):
    # Assume all C packages are well-behaved and install *.pc files.
    @property
    def provides_pkg_config(self) -> bool:
        return True

    @property
    def provides_shlibs(self) -> bool:
        return True

    @property
    def provides_c_headers(self) -> bool:
        return True

    def configure_dependency(
        self,
        build: targets.Build,
        dep: BasePackage,
        conf_args: Args,
        conf_env: Args,
        wd: str | None = None,
    ) -> None:
        if build.is_bundled(dep):
            build.sh_append_pkgconfig_paths(conf_env, dep, wd=wd)

            rel_path = build.sh_get_bundled_install_path(dep, wd=wd)
            ldflags = [f"-L{rel_path}/lib/"]

            if platform.system() != "Darwin":
                ldflags.append(f"-Wl,-rpath-link,{rel_path}/lib")

            build.sh_append_quoted_ldflags(conf_env, ldflags)

            bin_path = build.sh_get_bundled_pkg_bin_path(dep)
            if bin_path:
                build.sh_prepend_quoted_paths(conf_env, "PATH", [bin_path])

    def sh_get_configure_command(self, build: targets.Build) -> str:
        if self.supports_out_of_tree_builds:
            sdir = build.get_source_dir(self, relative_to="pkgbuild")
        else:
            sdir = build.get_build_dir(self, relative_to="pkgbuild")

        return shlex.quote((sdir / "configure").as_posix())

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        conf_args: Args = {}
        return conf_args

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env_args: Args = {}
        build.sh_append_run_time_ldflags(env_args, self)
        build.sh_append_link_time_ldflags(env_args, self, wd=wd)
        all_build_deps = build.get_build_reqs(self, recursive=True)
        return build.sh_append_global_flags(env_args) | build.get_ld_env(
            all_build_deps, wd=wd
        )

    def get_configure_script(self, build: targets.Build) -> str:
        script = super().get_configure_script(build)
        script += "_wd=$(pwd -P)\n"
        wd = "${_wd}"
        cmd = self.sh_get_configure_command(build)

        args = self.get_configure_args(build, wd=wd)
        env = self.get_configure_env(build, wd=wd)

        for build_dep in build.get_build_reqs(self, bundled_only=False):
            self.configure_dependency(build, build_dep, args, env, wd=wd)

        conf_script = build.sh_append_args(
            cmd, args, force_args_eq=True, linebreaks=False
        )

        if env:
            env_script = build.sh_format_command("env", env, force_args_eq=True)
            script += f"{env_script} {textwrap.indent(conf_script, '  ')}"
        else:
            script += conf_script

        return script

    def get_build_install_script(self, build: targets.Build) -> str:
        script = super().get_build_install_script(build)
        install_target = self.get_make_install_target(build)

        if install_target and not build.is_node_build:
            find = build.sh_get_command("find")
            sed = build.sh_get_command("sed")
            destdir = self.sh_get_make_install_destdir(build, "$(pwd)")
            libdir = self.get_install_path(build, "lib")
            re_libdir = re.escape(str(libdir))
            includedir = self.get_install_path(build, "include")
            re_includedir = re.escape(str(includedir))
            prefix = build.get_install_prefix(self)
            re_prefix = re.escape(str(prefix))
            script += "\n" + textwrap.dedent(
                f"""\
                _d={destdir}
                {find} "$_d" -name '*.la' -exec {sed} -i -r -e \
                    "s|{re_libdir}|${{_d}}{libdir}|g" {{}} \\;
                {find} "$_d" -path '*/pkgconfig/*.pc' -exec {sed} -i -r -e \
                    "s|includedir\\s*=.*|includedir=${{_d}}{includedir}|g
                     s|libdir\\s*=.*|libdir=${{_d}}{libdir}|g
                     s|exec_prefix\\s*=.*|exec_prefix=${{_d}}{prefix}|g
                    " {{}} \\;
                {find} "$_d" -path '*/cmake/*/*.cmake' -exec \
                    {sed} -i -r -e \
                    "s|_IMPORT_PREFIX\\s+\\"{re_prefix}\\"|\
_IMPORT_PREFIX \\"${{_d}}{prefix}\\"|g
                     s|(\\"\\|;){re_includedir}|\\1${{_d}}{includedir}|g
                     s|(\\"\\|;){re_libdir}|\\1${{_d}}{libdir}|g
                    " {{}} \\;
                """
            )
            if cfg := self.get_dep_pkg_config_script():
                script += "\n" + textwrap.dedent(
                    f"""\
                    {find} "$_d" -path '*/bin/{cfg}' -exec {sed} -i -r -e \
                        "s|includedir\\s*=.*|includedir=${{_d}}{includedir}|g
                         s|libdir\\s*=.*|libdir=${{_d}}{libdir}|g
                         s|exec_prefix\\s*=.*|exec_prefix=${{_d}}{prefix}|g
                         s|(-I){re_includedir}|\\1${{_d}}{includedir}|g
                         s|(-L){re_libdir}|\\1${{_d}}{libdir}|g
                        " {{}} \\;
                    """
                )

        return script


class BundledCAutoconfPackage(BundledCPackage):
    def sh_get_configure_command(self, build: targets.Build) -> str:
        configure = super().sh_get_configure_command(build)
        # On macOS, executing configure directly enters its /bin/sh shebang.
        # SIP strips DYLD_* variables when starting that protected system
        # shell, so configure test programs cannot load staged dependencies
        # whose install names point at the final prefix.  Keep the configure
        # script in ggbuild's selected shell, which is already running outside
        # that boundary and preserves the dependency loader environment.
        bash = build.sh_get_command("bash")
        return f"{bash} {configure}"

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env = super().get_configure_env(build, wd=wd)
        # Autoconf propagates CONFIG_SHELL to recursive configure scripts and
        # uses it when re-executing itself.  This keeps the whole configure
        # tree out of macOS's SIP-protected /bin/sh boundary, where DYLD_*
        # variables would otherwise be removed.
        env["CONFIG_SHELL"] = build.sh_get_command("bash")
        # Some Darwin environments leave this Autoconf probe empty.  Libtool
        # then combines objects through partial links, which can hide local
        # definitions from references in the resulting dylib.
        if platform.system() == "Darwin":
            env["lt_cv_sys_max_cmd_len"] = "1048576"
        return env

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        return super().get_configure_args(build, wd=wd) | {
            "--prefix": build.get_install_prefix(self),
            "--bindir": build.get_install_path(self, "bin"),
            "--sbindir": build.get_install_path(self, "bin"),
            "--sysconfdir": build.get_install_path(self, "sysconf"),
            "--localstatedir": build.get_install_path(self, "localstate"),
            "--libdir": build.get_install_path(self, "lib"),
            "--includedir": build.get_install_path(self, "include"),
            "--datarootdir": build.get_install_path(self, "data"),
            "--docdir": build.get_install_path(self, "doc"),
            "--mandir": build.get_install_path(self, "man"),
            "--infodir": build.get_install_path(self, "info"),
        }

    def configure_dependency(
        self,
        build: targets.Build,
        dep: BasePackage,
        conf_args: Args,
        conf_env: Args,
        wd: str | None = None,
    ) -> None:
        super().configure_dependency(build, dep, conf_args, conf_env, wd=wd)
        try:
            pkg_config_meta = self.get_dep_pkg_config_meta(dep)
        except poetry_repo_exc.PackageNotFoundError:
            # This is a preinstalled system build-time package,
            # for which we have no in-tree definition.
            return

        var_prefix = pkg_config_meta.pkg_name
        if build.is_bundled(dep):
            cppflags = build.sh_get_bundled_pkg_cppflags(dep, wd=wd)
            if cppflags:
                build.sh_append_quoted_flags(
                    conf_env,
                    "CPPFLAGS",
                    cppflags,
                )

            if not pkg_config_meta.provides_pkg_config:
                dep_ldflags = build.sh_get_bundled_pkg_ldflags(dep, wd=wd)

                for shlib in dep.get_shlibs(build):
                    dep_ldflags.append(f"-l{shlex.quote(shlib)}")

                transitive_deps = build.get_build_reqs(dep, recursive=True)
                transitive_cflags = build.sh_get_bundled_pkgs_cflags(
                    transitive_deps
                )

                rel_path = build.sh_get_bundled_install_path(
                    dep, relative_to="pkgbuild", wd=wd
                )

                if var_prefix:
                    build.sh_append_quoted_flags(
                        conf_args,
                        f"{var_prefix}_CFLAGS",
                        [f"-I{rel_path}/include", *transitive_cflags],
                    )
                    build.sh_append_quoted_flags(
                        conf_args,
                        f"{var_prefix}_LIBS",
                        dep_ldflags,
                    )
                else:
                    build.sh_append_quoted_flags(
                        conf_args,
                        "CFLAGS",
                        [f"-I{rel_path}/include", *transitive_cflags],
                    )
                    build.sh_append_quoted_ldflags(
                        conf_args,
                        dep_ldflags,
                    )

        elif build.is_stdlib(dep) and pkg_config_meta.provides_pkg_config:
            conf_args[f"{var_prefix}_CFLAGS"] = f"-D_{var_prefix}_IS_SYSLIB"
            std_ldflags = [f"-l{shlib}" for shlib in dep.get_shlibs(build)]
            conf_args[f"{var_prefix}_LIBS"] = build.sh_join_flags(std_ldflags)


class BundledCMesonPackage(BundledCPackage):
    def sh_get_configure_command(self, build: targets.Build) -> str:
        sdir = str(build.get_source_dir(self, relative_to="pkgbuild"))
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        meson = build.sh_get_command("meson")
        return f"{meson} setup {shlex.quote(sdir)} {shlex.quote(bdir)}"

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        return {
            "--prefix": build.get_install_prefix(self),
            "--sysconfdir": build.get_install_path(self, "sysconf"),
            "--bindir": build.get_install_path(self, "bin"),
            "--sbindir": build.get_install_path(self, "bin"),
            "--libdir": build.get_install_path(self, "lib"),
            "--localstatedir": build.get_install_path(self, "localstate"),
            "--includedir": build.get_install_path(self, "include"),
            # Meson does not support --docdir
            # "--docdir": build.get_install_path(self, "doc"),
            "--mandir": build.get_install_path(self, "man"),
            "--infodir": build.get_install_path(self, "info"),
            "-Ddefault_library": "shared",
        }

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env_args = dict(super().get_configure_env(build, wd))
        build.sh_append_run_time_ldflags(env_args, self)
        return build.sh_append_global_flags(env_args)

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str | Sequence[str] = "",
    ) -> str:
        wd = "${_wd}"

        env = build.sh_format_command(
            "env",
            self.get_build_env(build, wd=wd),
            force_args_eq=True,
            linebreaks=False,
        )

        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        meson_args: Args = {
            "compile": None,
            "-C": bdir,
        }
        ninja_args = {
            f"-j{build.build_parallelism}": None,
            "--verbose": None,
        } | args
        ninja_args_line = build.sh_format_args(
            ninja_args, force_args_eq=True, linebreaks=False
        )
        build.sh_append_quoted_flags(
            meson_args,
            "--ninja-args",
            [ninja_args_line],
        )
        if isinstance(target, str):
            if target:
                meson_args[target] = None
        else:
            meson_args.update(dict.fromkeys(target))

        meson_compile = build.sh_get_command(
            "meson",
            args=meson_args,
            force_args_eq=False,
        )

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {meson_compile}
            """
        )

    def get_build_install_script(self, build: targets.Build) -> str:
        script = BundledPackage.get_build_install_script(self, build)
        meson = build.sh_get_command("meson")
        destdir = self.sh_get_make_install_destdir(build, "$(pwd)")
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        script += "\n" + textwrap.dedent(
            f"""\
            {meson} install -C {shlex.quote(bdir)} \
                --destdir={destdir} --no-rebuild
            """
        )

        return script


CMakeTargetBuildSystem = Literal["make", "ninja"]


class BundledCMakePackage(BundledCPackage):
    def sh_get_configure_command(self, build: targets.Build) -> str:
        srcdir = str(build.get_source_dir(self, relative_to="pkgbuild"))
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        return build.sh_get_command(
            "cmake", args={"-S": srcdir, "-B": bdir}, linebreaks=False
        )

    def get_target_build_system(
        self,
        build: targets.Build,
    ) -> CMakeTargetBuildSystem:
        return "make"

    def get_configure_script(self, build: targets.Build) -> str:
        build_rules_path = str(
            build.get_build_dir(self, relative_to="fsroot")
            / "ggbuild_rules.cmake"
        )
        build_rules: list[str] = []
        config_path = "ggbuild_common_config.cmake"
        config = []

        buildsys = self.get_target_build_system(build)
        if buildsys == "make":
            make = build.sh_get_command("make")
            config.append(
                f'set(CMAKE_MAKE_PROGRAM "{make}" CACHE PATH "path to make")'
            )
        elif buildsys == "ninja":
            ninja = build.sh_get_command("ninja")
            config.append(
                f'set(CMAKE_MAKE_PROGRAM "{ninja}" CACHE PATH "path to ninja")'
            )
        else:
            raise AssertionError(f"unexpected target build system: {buildsys}")

        config.extend((
            (
                'set(CMAKE_PREFIX_PATH "$ENV{CMAKE_PREFIX_PATH}" '
                'CACHE STRING "" FORCE)'
            ),
            (
                "set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY "
                'CACHE STRING "" FORCE)'
            ),
            (
                f'set(CMAKE_INSTALL_PREFIX "{build.get_install_prefix(self)}" '
                'CACHE STRING "" FORCE)'
            ),
            (
                'set(CMAKE_INSTALL_BINDIR "'
                f'{build.get_rel_install_path(self, "bin")}" '
                f'CACHE PATH "Output directory for binaries")'
            ),
            (
                'set(CMAKE_INSTALL_LIBDIR "'
                f'{build.get_rel_install_path(self, "lib")}" '
                f'CACHE PATH "Output directory for libraries")'
            ),
            (
                'set(CMAKE_INSTALL_INCLUDEDIR "'
                f'{build.get_rel_install_path(self, "include")}" '
                f'CACHE PATH "Output directory for headers")'
            ),
            (
                'set(CMAKE_INSTALL_DATAROOTDIR "'
                f'{build.get_rel_install_path(self, "data")}" '
                f'CACHE PATH "Output directory for data")'
            ),
            (
                'set(CMAKE_INSTALL_DOCDIR "'
                f'{build.get_rel_install_path(self, "doc")}" '
                f'CACHE PATH "Output directory for documentation")'
            ),
            (
                'set(CMAKE_INSTALL_MANDIR "'
                f'{build.get_rel_install_path(self, "man")}" '
                f'CACHE PATH "Output directory for man pages")'
            ),
            (
                'set(CMAKE_INSTALL_INFODIR "'
                f'{build.get_rel_install_path(self, "info")}" '
                f'CACHE PATH "Output directory for info pages")'
            ),
        ))

        config.extend([
            (
                f'set(CMAKE_USER_MAKE_RULES_OVERRIDE "{build_rules_path}" '
                'CACHE FILEPATH "ggbuild override rules")'
            ),
            'set(BUILD_SHARED_LIBS ON CACHE BOOL "")',
            'set(Python3_FIND_UNVERSIONED_NAMES FIRST CACHE STRING "")',
            'set(CMAKE_DISABLE_PRECOMPILE_HEADERS ON CACHE BOOL "")',
            'set(CMAKE_TLS_VERIFY ON CACHE BOOL "")',
            'set(CMAKE_COMPILE_WARNING_AS_ERROR OFF CACHE BOOL "")',
        ])

        build_rules_text = textwrap.indent(
            "\n".join(build_rules), " " * 12
        ).lstrip()
        config_text = textwrap.indent("\n".join(config), " " * 12).lstrip()
        script = textwrap.dedent(
            f"""
            _wd=$(pwd -P)
            cat > "{build_rules_path}" <<- '_EOF_'
            {build_rules_text}
            _EOF_
            cat > "{config_path}" <<- '_EOF_'
            {config_text}
            _EOF_
            """
        )
        return script + super().get_configure_script(build)

    def get_configure_args(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        if self.get_target_build_system(build) == "make":
            generator = "Unix Makefiles"
        else:
            generator = "Ninja"

        return {
            "-Cggbuild_common_config.cmake": None,
            f"-G{generator}": None,
            "-DCMAKE_BUILD_TYPE": "ggbuild",
            "-DCMAKE_VERBOSE_MAKEFILE": "ON",
            "-DCMAKE_POLICY_DEFAULT_CMP0144": "NEW",
        }

    def get_configure_env(
        self,
        build: targets.Build,
        wd: str | None = None,
    ) -> Args:
        env_args = dict(super().get_configure_env(build, wd))
        build.sh_append_run_time_ldflags(env_args, self)
        return build.sh_append_global_flags(env_args)

    def get_build_command(
        self,
        build: targets.Build,
        args: Args,
        target: str | Sequence[str] = "",
    ) -> str:
        args |= {f"-j{build.build_parallelism}": None}
        if self.get_target_build_system(build) == "ninja":
            args |= {"--verbose": None}
        else:
            args |= {"V": "100"}

        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        cmake_args: Args = {
            "--build": None,
            bdir: None,
        }
        target_args = ""
        if isinstance(target, str) and target:
            cmake_args["--target"] = target
        elif target:
            cmake_args["--target"] = None
            target_args = f" {shlex.join(target)}"

        cmake = build.sh_get_command(
            "cmake", args=cmake_args, force_args_eq=True
        )
        cmake += target_args

        cmake += " -- "

        cmake_build = build.sh_append_args(
            cmake,
            args,
            force_args_eq=True,
        )

        env = build.sh_format_command(
            "env",
            self.get_build_env(build, wd="${_wd}"),
            force_args_eq=True,
            linebreaks=False,
        )

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {cmake_build}
            """
        )

    def get_build_install_command(
        self,
        build: targets.Build,
        args: Args,
        target: str,
    ) -> str:
        bdir = str(build.get_build_dir(self, relative_to="pkgbuild"))
        cmake_args: Args = {
            "--install": None,
            bdir: None,
            "--verbose": None,
        } | dict(args)

        cmake = build.sh_get_command(
            "cmake", args=cmake_args, force_args_eq=True
        )

        wd = "${_wd}"
        env_args = self.get_build_install_env(build, wd=wd)
        build.sh_append_quoted_flags(
            env_args,
            "DESTDIR",
            [self.sh_get_make_install_destdir(build, wd=wd)],
        )

        env = build.sh_format_args(
            env_args,
            force_args_eq=True,
            linebreaks=False,
        )

        return textwrap.dedent(
            f"""\
            _wd=$(pwd -P)
            {env} {cmake}
            """
        )

    def configure_dependency(
        self,
        build: targets.Build,
        dep: BasePackage,
        conf_args: Args,
        conf_env: Args,
        wd: str | None = None,
    ) -> None:
        super().configure_dependency(build, dep, conf_args, conf_env, wd=wd)

        if build.is_bundled(dep):
            var_prefix = self.get_dep_pkg_config_meta(dep).pkg_name
            rel_path = build.sh_get_bundled_install_path(dep, wd=wd)
            conf_args[f"-D{var_prefix}_ROOT"] = f"!{rel_path}"


_semver_phase_spelling_map = {
    poetry_pep440_segments.RELEASE_PHASE_ID_ALPHA: "alpha",
    poetry_pep440_segments.RELEASE_PHASE_ID_BETA: "beta",
}


def semver_pre_tag(version: poetry_pep440.PEP440Version) -> str:
    pre = version.pre
    if pre is not None:
        return _semver_phase_spelling_map.get(pre.phase, pre.phase)
    return ""


def pep440_to_semver(ver: poetry_version.Version) -> str:
    version_string = ver.release.to_string()

    pre = []

    if ver.pre:
        pre.append(f"{semver_pre_tag(ver)}.{ver.pre.number}")

    if ver.post:
        pre.append(f"{ver.post.phase}.{ver.post.number}")

    if ver.dev:
        pre.append(f"{ver.dev.phase}.{ver.dev.number}")

    if pre:
        version_string = f"{version_string}-{'.'.join(pre)}"

    if ver.local:
        if not isinstance(ver.local, tuple):
            raise TypeError("parsed local version must be a tuple")
        version_string += "+" + ".".join(map(str, ver.local))

    return version_string.lower()
