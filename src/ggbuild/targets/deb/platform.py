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
import textwrap

from poetry.core.packages import dependency as poetry_dep, package as poetry_pkg
from poetry.repositories import repository as poetry_repo

from ggbuild import packages as mpkg, tools
from ggbuild.targets import base as targets
from ggbuild.targets.package import SystemPackage

from . import build as debuild

if TYPE_CHECKING:
    from distro import distro


PACKAGE_MAP = {
    "icu": "libicu??",
    "icu-dev": "libicu-dev",
    "zlib": "zlib1g",
    "zlib-dev": "zlib1g-dev",
    "libxslt-dev": "libxslt1-dev",
    "pam": "libpam0g",
    "pam-dev": "libpam0g-dev",
    "python": "python3",
    "uuid": "libuuid1",
    "uuid-dev": "uuid-dev",
    "systemd-dev": "libsystemd-dev",
    "ncurses": "ncurses-bin",
    "libffi-dev": "libffi-dev",
    "openssl-dev": "libssl-dev",
    "libexpat": "libexpat?",
    "libexpat-dev": "libexpat?-dev",
    "libgeos": "libgeos-c1v?",
    "libgeotiff": "libgeotiff?",
    "libjson-c": "libjson-c?",
    "libsqlite3": "libsqlite3-?",
    "libtiff": "libtiff?",
    "libprotobuf-c": "libprotobuf-c?",
    "libgdal": "libgdal??",
    "libproj": "libproj??",
    "protoc-c": "protobuf-c-compiler",
}


GROUP_MAP = {
    "Applications/Databases": "database",
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
            raise ValueError(
                f"unexpected upstream version format: {upstream_ver}"
            )
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


def _debian_version_to_pep440(debver: str) -> str:
    m = re.match(
        r"""
        ^(?:(?P<epoch>\d+):)?(?P<upstream>[^-]+)(?:-(?P<debian>.*))?$
    """,
        debver,
        re.VERBOSE,
    )

    if not m:
        raise ValueError(f"unexpected debian package version: {debver}")

    epoch = m.group("epoch")
    upstream, is_extra = _translate_upstream_version(m.group("upstream"))
    version = f"{epoch}!{upstream}" if epoch else upstream

    debian_part = m.group("debian")
    if debian_part:
        separator = "." if is_extra else "+"
        version += separator + debian_part.translate(_version_trans)

    return version


class DebRepository(poetry_repo.Repository):
    def __init__(
        self,
        name: str = "deb",
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
                "apt-cache", "policy", system_name, errors_are_fatal=False
            )
        except subprocess.CalledProcessError:
            return ()
        else:
            policy = self._parse_apt_policy_output(output.strip())
            if not policy:
                return ()
            packages = []
            for pkgmeta in policy:
                for version in pkgmeta["versions"]:
                    norm_version = _debian_version_to_pep440(version)
                    pkg = SystemPackage(
                        name,
                        norm_version,
                        pretty_version=version,
                        system_name=pkgmeta["name"],
                    )
                    packages.append(pkg)

            return tuple(packages)

    def _parse_apt_policy_output(self, output: str) -> list[dict[str, Any]]:
        if not output:
            return []
        metas = []
        lines = output.split("\n")
        while lines:
            parsed_header = self._parse_policy_header(lines, output)
            if parsed_header is None:
                break
            meta, line_index = parsed_header
            lines = lines[line_index + 1 :]
            versions, version_index = self._parse_policy_versions(lines, output)
            meta["versions"] = versions
            lines = lines[version_index:]
            metas.append(meta)
        return metas

    @staticmethod
    def _parse_policy_header(
        lines: list[str], output: str
    ) -> tuple[dict[str, Any], int] | None:
        meta: dict[str, Any] = {}
        for line_index, raw_line in enumerate(lines):
            line = raw_line.strip()
            if not line:
                continue
            if "name" not in meta:
                if not line.endswith(":"):
                    raise RuntimeError(
                        f"cannot parse apt-cache policy output:\n{output}"
                    )
                meta["name"] = line[:-1]
                continue
            name, _, value = line.partition(":")
            if value := value.strip():
                meta[name.lower()] = value
            elif name.lower() == "version table":
                return meta, line_index
        return None

    @staticmethod
    def _parse_policy_versions(
        lines: list[str], output: str
    ) -> tuple[list[str], int]:
        versions: list[str] = []
        last_indent = -1
        for line_index, line in enumerate(lines):
            match = re.match(r"^((?:\s|\*)*)(.*)$", line)
            if match is None:
                raise RuntimeError(
                    f"cannot parse apt-cache policy output:\n{output}"
                )
            indent = len(match.group(1))
            if indent == 0:
                return versions, line_index
            if last_indent == -1 or indent < last_indent:
                versions.append(match.group(2).split(" ", maxsplit=1)[0])
            last_indent = indent
        return versions, len(lines)


class BaseDebTarget(targets.FHSTarget, targets.LinuxDistroTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        targets.FHSTarget.__init__(self, arch, libc=libc)
        targets.LinuxDistroTarget.__init__(
            self, distro_info=distro_info, arch=arch, libc=libc
        )

    def prepare(self) -> None:
        tools.cmd("apt-get", "update")

    def get_package_repository(self) -> poetry_repo.Repository:
        return DebRepository()

    def get_package_group(self, pkg: mpkg.BundledPackage) -> str:
        return GROUP_MAP.get(pkg.group, pkg.group)

    def get_arch_libdir(self) -> pathlib.Path:
        arch = tools.cmd("dpkg-architecture", "-qDEB_HOST_MULTIARCH").strip()
        return pathlib.Path("/usr/lib") / arch

    def get_builder(self) -> type[debuild.Build]:
        return debuild.Build

    def get_capabilities(self) -> list[str]:
        capabilities = super().get_capabilities()
        return [*capabilities, "systemd", "tzdata"]

    def get_resource_path(
        self, build: targets.Build, resource: str
    ) -> pathlib.Path | None:
        if resource == "systemd-units":
            return pathlib.Path("/lib/systemd/system")
        return super().get_resource_path(build, resource)

    def get_global_rules(self) -> str:
        return textwrap.dedent(
            """\
            export DH_VERBOSE=1
            export SHELL = /bin/bash
            dpkg_buildflags = \
                DEB_BUILD_MAINT_OPTIONS=$(DEB_BUILD_MAINT_OPTIONS) \
                dpkg-buildflags
        """
        )


class ModernDebianTarget(BaseDebTarget):
    def get_global_rules(self) -> str:
        return textwrap.dedent(
            """\
            export DH_VERBOSE=1
            export SHELL = /bin/bash
            export DEB_BUILD_MAINT_OPTIONS = hardening=+all
            dpkg_buildflags = \
                DEB_BUILD_MAINT_OPTIONS=$(DEB_BUILD_MAINT_OPTIONS) \
                dpkg-buildflags
        """
        )


class DebianStretchOrNewerTarget(ModernDebianTarget):
    pass


class UbuntuXenialOrNewerTarget(BaseDebTarget):
    pass


class UbuntuBionicOrNewerTarget(ModernDebianTarget):
    def __init__(
        self, distro_info: distro.InfoDict, arch: str, libc: str
    ) -> None:
        super().__init__(distro_info, arch, libc)
        if " " in self.distro["codename"]:
            # distro described in full, e,g, "Bionic Beaver",
            # normalize that to a single lowercase word as
            # per debian convention
            c = self.distro["codename"].split(" ")[0].lower()
            self.distro["codename"] = c


def get_specific_target(
    distro_info: distro.InfoDict, arch: str, libc: str
) -> BaseDebTarget:
    if distro_info["id"] == "debian":
        ver = int(distro_info["version_parts"]["major"])
        if ver >= 9:
            return DebianStretchOrNewerTarget(distro_info, arch, libc)
        raise NotImplementedError(
            f"{distro_info['id']} {distro_info['codename']} is not supported"
        )

    if distro_info["id"] == "ubuntu":
        major = int(distro_info["version_parts"]["major"])
        minor = int(distro_info["version_parts"]["minor"])

        if (major, minor) >= (18, 4):
            return UbuntuBionicOrNewerTarget(distro_info, arch, libc)
        if (major, minor) >= (16, 4):
            return UbuntuXenialOrNewerTarget(distro_info, arch, libc)
        raise NotImplementedError(
            f"{distro_info['id']} {distro_info['codename']} is not supported"
        )

    raise NotImplementedError(f"{distro_info['id']} is not supported")
