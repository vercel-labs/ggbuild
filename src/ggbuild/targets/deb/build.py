# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import Protocol, cast

import datetime
import json
import os
import pathlib
import shlex
import shutil
import stat
import subprocess
import tempfile
import textwrap

from cleo.io.outputs import stream_output as cleo_io_stream_output

from ggbuild import packages, targets, tools


class DebianTarget(Protocol):
    distro: dict[str, str]

    def get_package_group(self, pkg: packages.BundledPackage) -> str: ...

    def get_global_rules(self) -> str: ...


class Build(targets.Build):
    @property
    def debian_target(self) -> DebianTarget:
        return cast("DebianTarget", self._target)

    def prepare(self) -> None:
        super().prepare()

        self._pkgroot = self._droot / self._root_pkg.name_slot
        self._srcroot = self._pkgroot / self._root_pkg.name_slot
        self._debroot = self._srcroot / "debian"
        self._artifactroot = pathlib.Path("_artifacts")
        self._buildroot = self._artifactroot / "build"
        self._tmproot = self._artifactroot / "tmp"
        self._installroot = self._artifactroot / "install"

        self._debroot.mkdir(parents=True)
        (self._debroot / self._tmproot).mkdir(parents=True)

        self._bin_shims = self._root_pkg.get_bin_shims(self)

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
        if relative_to == "pkgsource":
            return pathlib.Path("..") / path
        if relative_to == "pkgbuild":
            return pathlib.Path("..") / ".." / ".." / path
        if relative_to == "helpers":
            return pathlib.Path("..") / ".." / path
        if relative_to == "fsroot":
            return (self.get_source_abspath() / path).resolve()
        raise ValueError(f"invalid relative_to argument: {relative_to}")

    def get_spec_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path("debian"), relative_to=relative_to)

    def get_helpers_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            pathlib.Path("debian") / "helpers", relative_to=relative_to
        )

    def get_source_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path(), relative_to=relative_to)

    def get_tarball_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(pathlib.Path(".."), relative_to=relative_to)

    def get_patches_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            pathlib.Path("debian") / "patches", relative_to=relative_to
        )

    def get_extras_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(
            pathlib.Path("debian") / "extras", relative_to=relative_to
        )

    def get_source_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            pathlib.Path(package.name),
            relative_to=relative_to,
            relative_to_package=relative_to_package,
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
            relative_to_package=relative_to_package,
        )

    def get_temp_root(
        self, *, relative_to: targets.Location = "sourceroot"
    ) -> pathlib.Path:
        return self.get_dir(self._tmproot, relative_to=relative_to)

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
            relative_to_package=relative_to_package,
        )

    def get_build_install_dir(
        self,
        package: packages.BasePackage,
        *,
        relative_to: targets.Location = "sourceroot",
        relative_to_package: packages.BasePackage | None = None,
    ) -> pathlib.Path:
        return self.get_dir(
            self._installroot / package.name,
            relative_to=relative_to,
            relative_to_package=relative_to_package,
        )

    def _get_tarball_tpl(self, package: packages.BasePackage) -> str:
        rp = self._root_pkg
        version = f"{rp.name}_{rp.version.text}"
        return f"{version}.orig-{package.name}{{part}}.tar{{comp}}"

    def _format_version(self) -> str:
        return packages.pep440_to_semver(self._root_pkg.version)

    def build(self) -> None:
        self.prepare_tools()
        self.prepare_tarballs()
        self.unpack_sources()
        if not isinstance(self._root_pkg, packages.PrePackagedPackage):
            self.prepare_patches()
            self._write_common_bits()
            self._write_control()
            self._write_changelog()
            self._write_rules()
            self._write_scriptlets()
        self._dpkg_buildpackage()

    def _write_common_bits(self) -> None:
        debsource = self._debroot / "source"
        debsource.mkdir()
        pathlib.Path(debsource / "format").write_text(
            "3.0 (quilt)\n", encoding="utf-8"
        )
        pathlib.Path(self._debroot / "compat").write_text(
            "10\n", encoding="utf-8"
        )

    @staticmethod
    def _relation_spec(label: str, values: list[str]) -> str:
        if not values:
            return ""
        return f"\n{label}:\n " + ",\n ".join(values)

    def _meta_package_specs(
        self, name: str, root_version: str
    ) -> tuple[list[str], list[str]]:
        specs = [
            textwrap.dedent(
                """\
                Package: {name}
                Architecture: any
                Description:
                 {description}
                Depends:
                 {dependencies}
                """
            ).format(
                name=meta.name,
                description=meta.description,
                dependencies=",\n ".join(
                    f"{dep_name}{f' ({dep_ver})' if dep_ver else ''}"
                    for dep_name, dep_ver in meta.dependencies.items()
                ),
            )
            for meta in self._root_pkg.get_meta_packages(self, root_version)
        ]
        replaces = []
        for transition in self._root_pkg.get_transition_packages(self):
            specs.append(
                textwrap.dedent(
                    """\
                    Package: {transition}
                    Architecture: any
                    Priority: optional
                    Description:
                     transitional package; can be safely removed;
                     use {name} instead
                    Depends:
                     {name} (= {version}), ${{misc:Depends}}
                    """
                ).format(transition=transition, name=name, version=root_version)
            )
            replaces.append(f"{transition} (<< {root_version})")
        return specs, replaces

    def _write_control(self) -> None:
        build_deps = ",\n ".join(
            f"{dep.system_name} (>= {dep.pretty_version})"
            for dep in self._build_deps
            if isinstance(dep, targets.SystemPackage)
        )

        deps = ",\n ".join(
            f"{dep.system_name} (>= {dep.pretty_version})"
            for dep in self._deps
            if isinstance(dep, targets.SystemPackage)
        )

        base_name = self._root_pkg.name
        name = self._root_pkg.name_slot

        if self._bin_shims:
            common_package = textwrap.dedent(
                """\
                Package: {name}-common
                Architecture: any
                Description:
                 Support files for {title}.
            """
            ).format(
                name=base_name,
                title=self._root_pkg.title,
            )
            deps += f",\n {base_name}-common (>= {self._root_pkg.version})"
        else:
            common_package = ""

        distro = self.debian_target.distro["codename"]
        root_version = f"{self._format_version()}-{self._revision}~{distro}"

        conflicts = self._root_pkg.get_conflict_packages(self, root_version)
        meta_pkg_specs, transition_replaces = self._meta_package_specs(
            name, root_version
        )
        replaces = [*conflicts, *transition_replaces]
        provides = self._root_pkg.get_provided_packages(self, root_version)
        relation_specs = "".join((
            self._relation_spec("Conflicts", conflicts),
            self._relation_spec("Replaces", replaces),
            self._relation_spec(
                "Provides", [f"{pkg} (= {ver})" for pkg, ver in provides]
            ),
        ))

        section = self.debian_target.get_package_group(self._root_pkg)
        if self._subdist and self._subdist != "stable":
            # This is how reprepro determines which Component this
            # package should go to.
            section = f"{section}/{self._subdist}"

        control = textwrap.dedent(
            """\
            Source: {name}
            Priority: optional
            Section: {section}
            Maintainer: {maintainer}
            Standards-Version: 4.1.5
            XCBS-GGBuild-Metadata: {metadata}
            Build-Depends:
             debhelper (>= 9~),
             dh-exec (>= 0.13~),
             dpkg-dev (>= 1.16.1~),
             {build_deps}

            Package: {name}
            Architecture: any
            Depends:
             {deps},
             ${{misc:Depends}},
             ${{shlibs:Depends}}{relation_specs}
            Description:
             {description}

            {common_pkg}

            {meta_pkgs}
        """
        ).format(
            name=name,
            deps=deps,
            build_deps=build_deps,
            relation_specs=relation_specs,
            section=section,
            description=self._root_pkg.description,
            maintainer="MagicStack Inc. <hello@magic.io>",
            common_pkg=common_package,
            meta_pkgs="\n\n".join(meta_pkg_specs),
            metadata=json.dumps(self._root_pkg.get_artifact_metadata(self)),
        )

        pathlib.Path(self._debroot / "control").write_text(
            control, encoding="utf-8"
        )

        # Make sure we don't export any shlibs from the bundle.
        pathlib.Path(self._debroot / f"{name}.shlibs").write_text(
            "", encoding="utf-8"
        )

    def _write_changelog(self) -> None:
        distro = self.debian_target.distro["codename"]
        changelog = textwrap.dedent(
            """\
            {name} ({version}) {distro}; urgency=medium

              * New version.

             -- {maintainer}  {date}
        """
        ).format(
            name=f"{self._root_pkg.name_slot}",
            version=f"{self._format_version()}-{self._revision}~{distro}",
            distro=distro,
            maintainer="MagicStack Inc. <hello@magic.io>",
            date=datetime.datetime.now(datetime.UTC).strftime(
                "%a, %d %b %Y %H:%M:%S %z"
            ),
        )

        pathlib.Path(self._debroot / "changelog").write_text(
            changelog, encoding="utf-8"
        )

    def _write_rules(self) -> None:
        shlib_paths = self._get_bundled_shlib_paths()
        if shlib_paths:
            shlib_paths_spec = ":".join(
                shlex.quote(str(p)) for p in shlib_paths
            )
            shlib_paths_opt = f"-l {shlib_paths_spec}"
        else:
            shlib_paths_opt = ""

        rules = textwrap.dedent(
            """\
            #!/usr/bin/make -f

            include /usr/share/dpkg/architecture.mk

            {target_global_rules}

            DPKG_EXPORT_BUILDFLAGS = 1
            include /usr/share/dpkg/buildflags.mk

            # Facilitate hierarchical profile generation on amd64 (#730134)
            ifeq ($(DEB_HOST_ARCH),amd64)
            CFLAGS+= -fno-omit-frame-pointer
            endif

            export DPKG_GENSYMBOLS_CHECK_LEVEL=4

            %:
            \tdh $@

            override_dh_auto_configure-indep: stamp/configure-build
            override_dh_auto_configure-arch: stamp/configure-build
            override_dh_auto_build-indep: stamp/build
            override_dh_auto_build-arch: stamp/build

            stamp/configure-build:
            \tmkdir -p stamp _artifacts
            \ttouch "$@"

            stamp/build: stamp/configure-build
            {build_steps}
            \ttouch "$@"

            override_dh_auto_install-arch:
            {install_extras}

            override_dh_strip:
            \t{strip_steps}

            override_dh_install-arch:
            {install_steps}

            override_dh_auto_clean:
            \trm -rf stamp

            override_dh_shlibdeps:
            \tdh_shlibdeps {shlib_paths}

            override_dh_builddeb:
            \tdh_builddeb -- -Zxz
        """
        ).format(
            name=self._root_pkg.name_slot,
            target_global_rules=self.debian_target.get_global_rules(),
            build_steps=self._write_script("complete"),
            install_extras=textwrap.indent(self._get_install_extras(), "\t"),
            install_steps=self._write_script("install", installable_only=True),
            strip_steps=(
                "dh_strip --automatic-dbgsym"
                if self._build_dbgsym
                else "dh_strip --no-automatic-dbgsym"
            ),
            shlib_paths=shlib_paths_opt,
        )

        with pathlib.Path(self._debroot / "rules").open(
            "w", encoding="utf-8"
        ) as f:
            f.write(rules)
            pathlib.Path(f.name).chmod(0o755)

    def _write_scriptlets(self) -> None:
        stagemap = {
            "before_install": "preinst",
            "after_install": "postinst",
            "before_uninstall": "prerm",
            "after_uninstall": "postrm",
        }

        for genstage, debstage in stagemap.items():
            script = self.get_script(genstage, installable_only=True)
            if script:
                stagefile = f"{self.root_package.name_slot}.{debstage}"
                spec_root = self.get_spec_root(relative_to="fsroot")
                with pathlib.Path(spec_root / stagefile).open(
                    "w", encoding="utf-8"
                ) as f:
                    print("#!/bin/bash\nset -e", file=f)
                    print(script, file=f)

    def _get_package_install_script(self, pkg: packages.BasePackage) -> str:
        source_root = self.get_source_root(relative_to="pkgbuild")
        install_dir = self.get_build_install_dir(pkg, relative_to="sourceroot")
        temp_dir = self.get_temp_dir(pkg, relative_to="sourceroot")

        il_script_text = self.render_package_script(pkg, "install_list")
        il_script = self.sh_write_bash_helper(
            f"_gen_install_list_{pkg.unique_name}.sh",
            il_script_text,
            relative_to="sourceroot",
        )

        nil_script_text = self.render_package_script(pkg, "no_install_list")
        nil_script = self.sh_write_bash_helper(
            f"_gen_no_install_list_{pkg.unique_name}.sh",
            nil_script_text,
            relative_to="sourceroot",
        )

        ignore_script_text = self.render_package_script(pkg, "ignore_list")
        ignore_script = self.sh_write_bash_helper(
            f"_gen_ignore_list_{pkg.unique_name}.sh",
            ignore_script_text,
            relative_to="sourceroot",
        )

        trim_install = self.sh_get_command(
            "trim-install", relative_to="sourceroot"
        )

        return textwrap.dedent(
            f"""
            pushd "{source_root}" >/dev/null

            {il_script} > "{temp_dir}/install"
            {nil_script} > "{temp_dir}/not-installed"
            {ignore_script} > "{temp_dir}/ignored"

            {trim_install} \\
                "{temp_dir}/install" \\
                "{temp_dir}/not-installed" \\
                "{temp_dir}/ignored" \\
                "{install_dir}" \\
                | sed -e "s/ /?/g" \\
                > "debian/{self._root_pkg.name_slot}.install"

            dh_install --sourcedir="{install_dir}"
            dh_missing --sourcedir="{install_dir}" --fail-missing

            popd >/dev/null
        """
        )

    def _get_bundled_shlib_paths(self) -> list[pathlib.Path]:
        paths = []

        for pkg in self._installable:
            path = pkg.get_install_path(self, "lib")
            if path is not None:
                paths.append(path)

        return paths

    def _get_install_extras(self) -> str:
        lines: list[str] = []
        symlinks: list[tuple[pathlib.Path, str]] = []

        extras_dir = self.get_extras_root(relative_to="fsroot")
        sys_bindir = self.get_bundle_install_path("systembin").relative_to("/")

        for pkg in self._installable:
            for path, content in pkg.get_service_scripts(self).items():
                directory = extras_dir / path.parent.relative_to("/")
                directory.mkdir(parents=True)
                with pathlib.Path(directory / path.name).open(
                    "w", encoding="utf-8"
                ) as f:
                    print(content, file=f)

            symlinks.extend(
                (
                    cmd.relative_to("/"),
                    f"{sys_bindir}/{cmd.name}{pkg.slot_suffix}",
                )
                for cmd in pkg.get_exposed_commands(self)
            )
        if self._write_install_links(symlinks):
            lines.append("dh_link")
        if self._bin_shims:
            lines.extend(self._install_bin_shims(extras_dir))

        return "\n".join(lines)

    def _write_install_links(
        self, symlinks: list[tuple[pathlib.Path, str]]
    ) -> bool:
        if not symlinks:
            return False
        spec_root = self.get_spec_root(relative_to="fsroot")
        links = spec_root / f"{self.root_package.name_slot}.links"
        links.write_text(
            "\n".join(
                f"{source} {destination}" for source, destination in symlinks
            ),
            encoding="utf-8",
        )
        return True

    def _install_bin_shims(self, extras_dir: pathlib.Path) -> list[str]:
        lines: list[str] = []
        extras_dir_rel = self.get_extras_root(relative_to="sourceroot")
        destination = self._debroot / f"{self._root_pkg.name}-common"
        sysbindir = self.get_bundle_install_path("systembin")
        executable = (
            stat.S_IRWXU
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
        for shim_path, data in self._bin_shims.items():
            bin_path = (sysbindir / shim_path).relative_to("/")
            install_path = extras_dir / bin_path
            install_path.parent.mkdir(parents=True, exist_ok=True)
            install_path.write_text(data, encoding="utf-8")
            install_path.chmod(executable)
            source = shlex.quote(str(extras_dir_rel / bin_path))
            dest_path = destination / bin_path
            lines.extend((
                f"mkdir -p {shlex.quote(str(dest_path.parent))}",
                f"cp -p {source} {shlex.quote(str(dest_path))}",
            ))
        return lines

    def _dpkg_buildpackage(self) -> None:
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"

        if isinstance(self._root_pkg, packages.PrePackagedPackage):
            workdir = self._srcroot
        else:
            workdir = self.get_source_abspath()

        output = self._io.output
        if not isinstance(output, cleo_io_stream_output.StreamOutput):
            raise TypeError("Debian builds require a stream output")

        tools.cmd(
            "apt-get",
            "update",
            env=env,
            cwd=str(workdir),
            stdout=output.stream,
            stderr=subprocess.STDOUT,
        )

        tools.cmd(
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "equivs",
            "devscripts",
            env=env,
            cwd=str(workdir),
            stdout=output.stream,
            stderr=subprocess.STDOUT,
        )

        tools.cmd(
            "mk-build-deps",
            "-t",
            "apt-get -y --no-install-recommends",
            "-i",
            str(self._debroot / "control"),
            env=env,
            cwd=tempfile.gettempdir(),
            stdout=output.stream,
            stderr=subprocess.STDOUT,
        )

        args = ["-us", "-uc", "--source-option=--create-empty-orig"]
        if not self._build_source:
            args.append("-b")

        tools.cmd(
            "dpkg-buildpackage",
            *args,
            cwd=str(workdir),
            stdout=output.stream,
            stderr=subprocess.STDOUT,
        )

        # Ubuntu likes to call their dbgsym packages ddebs,
        # whereas Debian tools, including reprepro like it
        # to just be a .deb.
        for changes in self._pkgroot.glob("*.changes"):
            with pathlib.Path(changes).open("r+", encoding="utf-8") as f:
                f.seek(0)
                patched = f.read().replace(".ddeb", ".deb")
                f.seek(0)
                f.write(patched)

    def package(self) -> None:
        archives = self.get_intermediate_output_dir(relative_to="fsroot")
        contents = {}

        for entry in self._pkgroot.iterdir():
            if not entry.is_dir():
                output_name = entry.name
                if entry.suffix == ".ddeb":
                    output_name = entry.stem + ".deb"
                elif entry.suffix not in {".deb", ".changes", ".buildinfo"}:
                    continue
                if entry.suffix == ".deb":
                    mime = "application/vnd.debian.binary-package"
                else:
                    mime = "text/plain"
                contents[output_name] = {
                    "type": mime,
                    "encoding": "identity",
                    "suffix": entry.suffix,
                }
                shutil.copy2(entry, archives / output_name)

        distro = self.debian_target.distro["codename"]
        root_version = f"{self._format_version()}-{self._revision}~{distro}"
        with pathlib.Path(archives / "build-metadata.json").open(
            "w", encoding="utf-8"
        ) as f:
            installref = f"{self._root_pkg.name_slot}={root_version}"
            json.dump(
                {
                    "installrefs": [installref],
                    "contents": contents,
                    "repository": "apt",
                    **self._root_pkg.get_artifact_metadata(self),
                },
                f,
            )
