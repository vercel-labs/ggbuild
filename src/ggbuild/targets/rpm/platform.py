# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Any,
)

import pathlib
import re
import subprocess

from poetry.repositories import repository as poetry_repo

from ggbuild import tools
from ggbuild.targets import base as targets
from ggbuild.targets.package import SystemPackage

from . import build as rpmbuild

if TYPE_CHECKING:
    from distro import distro
    from poetry.core.packages import (
        dependency as poetry_dep,
        package as poetry_pkg,
    )


PACKAGE_MAP = {
    "icu": "libicu",
    "icu-dev": "libicu-devel",
    "zlib": "zlib",
    "zlib-dev": "zlib-devel",
    "libxslt-dev": "libxslt-devel",
    "pam-dev": "pam-devel",
    "python": "python3",
    "uuid": "libuuid",
    "uuid-dev": "libuuid-devel",
    "systemd-dev": "systemd-devel",
    "openssl-dev": "openssl-devel",
    "libffi-dev": "libffi-devel",
    "libb2-dev": "libb2-devel",
    "libpcre2": "pcre2",
    "libpcre2-dev": "pcre2-devel",
    "libxml2-dev": "libxml2-devel",
    "libexpat": "expat",
    "libexpat-dev": "expat-devel",
    "libgeos": "geos",
    "libgeos-dev": "geos-devel",
    "libgeotiff-dev": "libgeotiff-devel",
    "libjson-c": "json-c",
    "libjson-c-dev": "json-c-devel",
    "libjpeg": "libjpeg-turbo",
    "libjpeg-dev": "libjpeg-turbo-devel",
    "libsqlite3": "sqlite-libs",
    "libsqlite3-dev": "sqlite-devel",
    "libtiff-dev": "libtiff-devel",
    "libprotobuf-c": "protobuf-c",
    "libprotobuf-c-dev": "protobuf-c-devel",
    "libgdal": "gdal",
    "libgdal-dev": "gdal-devel",
    "libproj": "proj",
    "libproj-dev": "proj-devel",
    "protoc-c": "protobuf-c-compiler",
}


SYSTEM_DEPENDENCY_MAP = {
    "adduser": ["/usr/sbin/useradd", "/usr/sbin/groupadd"],
}


_version_trans = str.maketrans({"+": ".", "-": ".", "~": "."})


def _translate_upstream_version(upstream_ver: str) -> tuple[str, bool]:
    translated: list[str] = []
    is_extra = False
    for index, part in enumerate(upstream_ver.split(".")):
        if is_extra:
            translated.extend((".", part.translate(_version_trans)))
            continue
        part_match = re.fullmatch(r"([0-9]*)([A-Za-z]*)(.*)", part)
        if part_match is None:
            raise ValueError(f"unexpected RPM package version: {upstream_ver}")
        numeric, letters, rest = part_match.groups()
        if numeric:
            if index:
                translated.append(".")
            translated.append(numeric)
        translated.extend(f".{ord(char)}" for char in letters)
        if rest:
            translated.extend((
                "+",
                rest.lstrip("+-~").translate(_version_trans),
            ))
            is_extra = True
    return "".join(translated), is_extra


def _rpm_version_to_pep440(rpmver: str) -> str:
    m = re.match(
        r"""
        ^(?:(?P<epoch>\d+):)?(?P<upstream>[^-]+)(?:-(?P<rpm>.*))?$
    """,
        rpmver,
        re.VERBOSE,
    )

    if not m:
        raise ValueError(f"unexpected RPM package version: {rpmver}")

    version, is_extra = _translate_upstream_version(m.group("upstream"))

    rpm_part = m.group("rpm")
    if rpm_part:
        separator = "." if is_extra else "+"
        version += separator + rpm_part.translate(_version_trans)

    return version


class RPMRepository(poetry_repo.Repository):
    def __init__(
        self,
        name: str = "rpm",
        packages: list[poetry_pkg.Package] | None = None,
    ) -> None:
        super().__init__(name, packages)
        self._parsed: set[str] = set()

    def find_packages(
        self,
        dependency: poetry_dep.Dependency,
    ) -> list[poetry_pkg.Package]:
        if dependency.name not in self._parsed:
            packages = self.apt_get_packages(dependency.name)
            for package in packages:
                self.add_package(package)
            self._parsed.add(dependency.name)

        return super().find_packages(dependency)

    def apt_get_packages(self, name: str) -> tuple[poetry_pkg.Package, ...]:
        system_name = PACKAGE_MAP.get(name, name)

        try:
            output = tools.cmd(
                "yum",
                "--showduplicates",
                "list",
                system_name,
                errors_are_fatal=False,
                hide_stderr=True,
            )
        except subprocess.CalledProcessError:
            return ()
        else:
            policy = self._parse_yum_list_output(output.strip())
            if not policy:
                return ()
            packages = []
            for version in policy["versions"]:
                norm_version = _rpm_version_to_pep440(version)
                pkg = SystemPackage(
                    name,
                    norm_version,
                    pretty_version=version,
                    system_name=system_name,
                )
                packages.append(pkg)

            return tuple(packages)

    def _parse_yum_list_output(self, output: str) -> dict[str, Any]:
        if not output:
            return {}

        meta = {}

        lines = output.split("\n")

        line_index = 0
        for current_index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if line == "Available Packages":
                line_index = current_index
                break
        else:
            return {}

        versions = []

        for line in lines[line_index + 1 :]:
            cols = re.split(r"\s+", line)
            if cols[1] not in versions:
                versions.append(cols[1])

        meta["versions"] = versions

        return meta


class BaseRPMTarget(targets.FHSTarget, targets.LinuxDistroTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        targets.FHSTarget.__init__(self, arch, libc=libc)
        targets.LinuxDistroTarget.__init__(
            self, distro_info=distro_info, arch=arch, libc=libc
        )

    def get_package_repository(self) -> RPMRepository:
        return RPMRepository()

    def get_arch_libdir(self) -> pathlib.Path:
        return pathlib.Path(tools.cmd("rpm", "--eval", "%_libdir").strip())

    def get_sys_bindir(self) -> pathlib.Path:
        return pathlib.Path(tools.cmd("rpm", "--eval", "%_bindir").strip())

    def get_builder(self) -> type[rpmbuild.Build]:
        return rpmbuild.Build

    def get_system_dependencies(self, dep_name: str) -> list[str]:
        try:
            return SYSTEM_DEPENDENCY_MAP[dep_name]
        except KeyError:
            return super().get_system_dependencies(dep_name)

    def install_build_deps(self, build: rpmbuild.Build, spec: str) -> None:
        tools.cmd(
            "yum",
            "install",
            "-y",
            "rpm-build",
            "rpmlint",
            "yum-utils",
            stdout=build.stream,
            stderr=subprocess.STDOUT,
        )

        tools.cmd(
            "yum-builddep",
            "-y",
            spec,
            cwd=str(build.get_spec_root(relative_to="fsroot")),
            stdout=build.stream,
            stderr=subprocess.STDOUT,
        )


class RHEL7OrNewerTarget(BaseRPMTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        super().__init__(distro_info, arch, libc)
        self.distro["codename"] = f"el{self.distro['version_parts']['major']}"

    def get_capabilities(self) -> list[str]:
        capabilities = super().get_capabilities()
        return [*capabilities, "systemd", "tzdata"]

    def get_resource_path(
        self, build: targets.Build, resource: str
    ) -> pathlib.Path | None:
        if resource == "systemd-units":
            return pathlib.Path(tools.cmd("rpm", "--eval", "%_unitdir").strip())
        return super().get_resource_path(build, resource)


class RHEL9OrNewerTarget(RHEL7OrNewerTarget):
    def install_build_deps(self, build: rpmbuild.Build, spec: str) -> None:
        super().install_build_deps(build, spec)
        tools.cmd(
            "yum",
            "install",
            "-y",
            "systemd-rpm-macros",  # for %_unitdir
            stdout=build.stream,
            stderr=subprocess.STDOUT,
        )


class FedoraTarget(RHEL7OrNewerTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        super().__init__(distro_info, arch, libc)
        self.distro["codename"] = f"fc{self.distro['version_parts']['major']}"

    def install_build_deps(self, build: rpmbuild.Build, spec: str) -> None:
        tools.cmd(
            "dnf",
            "builddep",
            "-y",
            spec,
            cwd=str(build.get_spec_root(relative_to="fsroot")),
            stdout=build.stream,
            stderr=subprocess.STDOUT,
        )


class AmazonLinuxTarget(RHEL7OrNewerTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        super().__init__(distro_info, arch, libc)
        self.distro["codename"] = f"amzn{self.distro['version_parts']['major']}"

    def install_build_deps(self, build: rpmbuild.Build, spec: str) -> None:
        tools.cmd(
            "dnf",
            "builddep",
            "-y",
            spec,
            cwd=str(build.get_spec_root(relative_to="fsroot")),
        )


def get_specific_target(
    distro_info: distro.InfoDict, arch: str, libc: str
) -> BaseRPMTarget:
    if distro_info["id"] in {"centos", "rhel", "rocky"}:
        ver = int(distro_info["version_parts"]["major"])
        if ver >= 9:
            return RHEL9OrNewerTarget(distro_info, arch, libc)
        if ver >= 7:
            return RHEL7OrNewerTarget(distro_info, arch, libc)
        raise NotImplementedError(
            f"{distro_info['id']} {distro_info['codename']} is not supported"
        )

    if distro_info["id"] == "fedora":
        ver = int(distro_info["version_parts"]["major"])
        if ver < 29:
            raise NotImplementedError(
                f"{distro_info['id']} {distro_info['codename']} "
                "is not supported"
            )
        return FedoraTarget(distro_info, arch, libc)

    if distro_info["id"] == "amzn":
        ver = int(distro_info["version_parts"]["major"])
        if ver < 2023:
            raise NotImplementedError(
                f"{distro_info['id']} {distro_info['version']} is not supported"
            )
        return AmazonLinuxTarget(distro_info, arch, libc)

    raise NotImplementedError(f"{distro_info['id']} is not supported")
