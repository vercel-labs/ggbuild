# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import json
import logging
import mimetypes
import os
import pathlib
import shutil
import stat

from defusedxml import minidom

from ggbuild import tools
from ggbuild.targets import generic

logger = logging.getLogger(__name__)


class MacOSBuild(generic.Build):
    def define_tools(self) -> None:
        super().define_tools()
        bash = self._find_tool("bash")
        self._system_tools["bash"] = bash
        gmake = self._find_tool("gmake")
        self._system_tools["make"] = gmake
        self._system_tools["sed"] = self._find_tool("gsed")
        self._system_tools["tar"] = self._find_tool("gtar")
        self._system_tools["cmake"] = self._find_tool("cmake")
        self._system_tools["ninja"] = self._find_tool("ninja")

    def _find_tool(self, tool: str) -> str:
        tool_path = shutil.which(tool)
        if tool_path is None:
            raise RuntimeError(f"required program not found: {tool}")
        return tool_path

    def _fixup_rpath(
        self,
        image_root: pathlib.Path,
        binary_relpath: pathlib.Path,
        *,
        additional_rpaths: set[pathlib.Path] | None = None,
    ) -> None:
        inst_prefix = self.get_bundle_install_prefix()
        full_path = image_root / binary_relpath
        inst_path = pathlib.Path("/") / binary_relpath
        shlibs, existing_rpaths = self.target.get_shlib_refs(
            self, image_root, binary_relpath, resolve=False
        )
        requested_rpaths = existing_rpaths | (additional_rpaths or set())
        rpaths, shlib_alters = self._normalized_rpaths(
            shlibs, requested_rpaths, inst_prefix, inst_path
        )

        args: list[str | pathlib.Path] = []
        for added in rpaths - existing_rpaths:
            args.extend(("-add_rpath", added))

        for old, new in shlib_alters:
            args.extend(("-change", old, new))

        if args:
            args.append(full_path)

            tools.cmd(
                "install_name_tool",
                *args,
            )

        for removed in existing_rpaths - rpaths:
            present_rpaths = existing_rpaths
            # Unfortunately, macOS ld creates duplicate LC_RPATH
            # entries (from duplicate -rpath command line arguments),
            # and install_name_tool only removes the _first_ matching
            # entry rather than all of them.
            while removed in present_rpaths:
                tools.cmd(
                    "install_name_tool",
                    "-delete_rpath",
                    removed,
                    full_path,
                )
                _, present_rpaths = self.target.get_shlib_refs(
                    self, image_root, binary_relpath, resolve=False
                )

    @staticmethod
    def _normalized_rpaths(
        shlibs: set[pathlib.Path],
        existing_rpaths: set[pathlib.Path],
        inst_prefix: pathlib.Path,
        inst_path: pathlib.Path,
    ) -> tuple[set[pathlib.Path], set[tuple[str, str]]]:
        rpaths = set()
        shlib_alters: set[tuple[str, str]] = set()
        for rpath in existing_rpaths:
            if rpath.parts[0] == "@loader_path":
                rpaths.add(rpath)
                continue
            if not rpath.is_relative_to(inst_prefix):
                logger.info(
                    "RPATH %s is outside the install image; removing", rpath
                )
                continue
            relative_rpath = pathlib.Path("@loader_path") / os.path.relpath(
                rpath, start=inst_path.parent
            )
            rpaths.add(relative_rpath)
            shlib_alters.update(
                (
                    str(shlib),
                    str(pathlib.Path("@rpath") / shlib.relative_to(rpath)),
                )
                for shlib in shlibs
                if shlib.is_relative_to(rpath)
            )
        return rpaths, shlib_alters


class NativePackageBuild(MacOSBuild):
    def _build_common_package(
        self, installer: pathlib.Path, version: str
    ) -> pathlib.Path:
        selectdir = installer / "Common"
        selectdir.mkdir(parents=True)
        sysbindir = self.get_bundle_install_path("systembin")
        for path, data in self._root_pkg.get_bin_shims(self).items():
            bin_path = (sysbindir / path).relative_to("/")
            inst_path = selectdir / bin_path
            inst_path.parent.mkdir(parents=True, exist_ok=True)
            inst_path.write_text(data, encoding="utf-8")
            inst_path.chmod(
                stat.S_IRWXU
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
        paths_d = selectdir / "etc" / "paths.d" / self._root_pkg.identifier
        paths_d.parent.mkdir(parents=True)
        paths_d.write_text(f"{sysbindir}\n", encoding="utf-8")
        common_pkgpath = installer / f"{self._root_pkg.name}-common.pkg"
        tools.cmd(
            "pkgbuild",
            "--root",
            selectdir,
            "--identifier",
            f"{self._root_pkg.identifier}-common",
            "--version",
            version,
            "--install-location",
            "/",
            common_pkgpath,
        )
        return common_pkgpath

    def _build_main_package(
        self, installer: pathlib.Path, ident: str, version: str
    ) -> pathlib.Path:
        scriptdir = installer / "Scripts"
        scriptdir.mkdir(parents=True)
        for genstage, inststage in {
            "before_install": "preinstall",
            "after_install": "postinstall",
        }.items():
            script = self.get_script(genstage, installable_only=True)
            if script:
                script_path = scriptdir / inststage
                script_path.write_text(
                    f"#!/bin/bash\nset -e\n{script}\n", encoding="utf-8"
                )
                script_path.chmod(
                    stat.S_IRWXU
                    | stat.S_IRGRP
                    | stat.S_IXGRP
                    | stat.S_IROTH
                    | stat.S_IXOTH
                )
        pkgpath = (
            installer / f"{self._root_pkg.name}{self._root_pkg.slot_suffix}.pkg"
        )
        tools.cmd(
            "pkgbuild",
            "--root",
            self.get_image_root(relative_to="fsroot"),
            "--identifier",
            ident,
            "--scripts",
            scriptdir,
            "--version",
            version,
            "--install-location",
            "/",
            pkgpath,
        )
        return pkgpath

    def _write_resources(
        self, installer: pathlib.Path, version: str
    ) -> tuple[pathlib.Path, dict[str, bytes]]:
        rsrcdir = installer / "Resources"
        rsrcdir.mkdir(parents=True)
        resources = self._root_pkg.get_resources(self)
        nice_title = self._root_pkg.title or self._root_pkg.name
        for name, resource_data in resources.items():
            rendered_data = resource_data.replace(
                b"$TITLE", nice_title.encode()
            ).replace(b"$FULL_VERSION", version.encode())
            (rsrcdir / name).write_bytes(rendered_data)
        return rsrcdir, resources

    def _write_distribution(
        self,
        installer: pathlib.Path,
        pkgpath: pathlib.Path,
        common_pkgpath: pathlib.Path,
        rsrcdir: pathlib.Path,
        resources: dict[str, bytes],
        ident: str,
        version: str,
    ) -> pathlib.Path:
        distribution = installer / "Distribution.xml"
        tools.cmd(
            "productbuild",
            "--package",
            pkgpath,
            "--package",
            common_pkgpath,
            "--resources",
            rsrcdir,
            "--identifier",
            ident,
            "--version",
            version,
            "--synthesize",
            distribution,
        )
        dist_xml = minidom.parse(str(distribution))
        gui_xml = dist_xml.documentElement
        if gui_xml is None:
            raise RuntimeError("distribution XML has no document element")
        for name in resources:
            res_type = pathlib.Path(name).stem.lower()
            if res_type in {
                "welcome",
                "readme",
                "license",
                "conclusion",
                "background",
            }:
                mimetype = mimetypes.guess_type(name)
                element = dist_xml.createElement(res_type)
                element.setAttribute("file", name)
                if mimetype[0] is not None:
                    element.setAttribute("mime-type", mimetype[0])
                if res_type == "background":
                    element.setAttribute("alignment", "left")
                gui_xml.appendChild(element)
        title_el = dist_xml.createElement("title")
        title_text = dist_xml.createTextNode(
            self._root_pkg.title or "<no title>"
        )
        title_el.appendChild(title_text)
        gui_xml.appendChild(title_el)
        if options_els := gui_xml.getElementsByTagName("options"):
            options = options_els[0]
        else:
            options = dist_xml.createElement("options")
            gui_xml.appendChild(options)
        options.setAttribute("customize", "never")
        options.setAttribute("rootVolumeOnly", "true")
        distribution.write_text(dist_xml.toprettyxml(), encoding="utf-8")
        return distribution

    def _package(self, files: list[pathlib.Path]) -> None:
        pkg = self._root_pkg
        version = pkg.pretty_version
        ident = f"{pkg.identifier}{pkg.slot_suffix}"
        installer = self.get_temp_root(relative_to="fsroot") / "installer"
        installer.mkdir(parents=True)
        common_pkgpath = self._build_common_package(installer, version)
        pkgpath = self._build_main_package(installer, ident, version)
        rsrcdir, resources = self._write_resources(installer, version)
        distribution = self._write_distribution(
            installer,
            pkgpath,
            common_pkgpath,
            rsrcdir,
            resources,
            ident,
            version,
        )
        archives = self.get_intermediate_output_dir(relative_to="fsroot")
        suffix = (
            f"{self._revision}~{self._subdist}"
            if self._subdist
            else self._revision
        )
        root_version = f"{pkg.slot_suffix}_{version}_{suffix}"
        finalname = f"{pkg.name}{root_version}.pkg"
        tools.cmd(
            "productbuild",
            "--package-path",
            pkgpath.parent,
            "--resources",
            rsrcdir,
            "--identifier",
            ident,
            "--version",
            version,
            "--distribution",
            distribution,
            archives / finalname,
        )
        with (archives / "build-metadata.json").open(
            "w", encoding="utf-8"
        ) as f:
            json.dump(
                {
                    "installrefs": [finalname],
                    **self._root_pkg.get_artifact_metadata(self),
                },
                f,
            )


class GenericMacOSBuild(MacOSBuild):
    pass
