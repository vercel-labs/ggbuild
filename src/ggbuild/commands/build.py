# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import ClassVar, cast

import collections
import contextlib
import dataclasses
import datetime
import graphlib
import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile
from collections.abc import Iterator

from cleo.helpers import argument, option
from lograil import stage
from poetry import puzzle
from poetry.core.packages import dependency as poetry_dep, project_package
from poetry.repositories import repository_pool as poetry_repository_pool
from poetry.utils import env as poetry_env

from ggbuild import cache as ggbuild_cache, targets
from ggbuild.packages import (
    base as mpkg_base,
    python as af_python,
    repository as af_repo,
)
from ggbuild.project import load_project
from ggbuild.scheduler import (
    RunOptions,
    format_summary,
    parse_node_cache,
    run_local,
)

from . import base


class _Solver(puzzle.Solver):
    """Poetry solver without its competing terminal indicator."""

    @contextlib.contextmanager
    def _progress(self) -> Iterator[None]:
        yield


@dataclasses.dataclass(frozen=True)
class _BuildOptions:
    package: str
    destination: str
    work_dir: str | None
    keepwork: bool
    clean: bool
    generic: bool
    arch: str | None
    libc: str | None
    build_source: bool
    build_dbgsym: bool
    version: str | None
    revision: str | None
    subdist: str | None
    is_release: bool
    extra_opt: bool
    jobs: int
    tags: dict[str, str]
    compression: list[str]
    enable_sccache: bool


class Build(base.Command):
    name = "build"
    description = """Builds the specified package on the current platform."""
    arguments: ClassVar = [
        argument(
            "name",
            description="Package to build",
            optional=True,
        ),
    ]
    options: ClassVar = [
        option(
            "target",
            description="Configured project target triple.",
            flag=False,
        ),
        option(
            "jobs",
            description="Use up to N processes in parallel to build.",
            flag=False,
        ),
        option(
            "dest",
            description="Destination path.",
            flag=False,
        ),
        option(
            "work-dir",
            description="Build work directory.",
            flag=False,
        ),
        option(
            "keepwork",
            description=(
                "Retain the work directory and resume completed stages on "
                "subsequent invocations."
            ),
            flag=True,
        ),
        option(
            "clean",
            description=(
                "Rebuild all nodes as if --node-cache='*=ignore' were set."
            ),
            flag=True,
        ),
        option(
            "generic",
            description="Build a generic artifact.",
            flag=True,
        ),
        option(
            "arch",
            description="Target architecture, if different from host.",
            flag=False,
        ),
        option(
            "libc",
            description="Libc to target.",
            flag=False,
        ),
        option(
            "build-source",
            description="Build source packages.",
            flag=True,
        ),
        option(
            "build-dbgsym",
            description="Build separate debug-symbol archives.",
            flag=True,
        ),
        option(
            "release",
            description="Whether this build is a release.",
            flag=True,
        ),
        option(
            "source-ref",
            description="Source version to build (VCS ref or tarball version).",
            flag=False,
        ),
        option(
            "pkg-revision",
            description="Override package revision number (defaults to 1).",
            flag=False,
        ),
        option(
            "pkg-subdist",
            description="Set package sub-distribution (e.g. nightly).",
            flag=False,
        ),
        option(
            "pkg-tags",
            description=(
                "Comma-separated list of key=value pairs to include "
                "in package metadata."
            ),
            flag=False,
        ),
        option(
            "pkg-compression",
            description=(
                "Comma-separated list of compression encodings to apply "
                "if building for the specified target produces a tarball "
                "(defaults to zstd)."
            ),
            flag=False,
        ),
        option(
            "extra-optimizations",
            description="Enable extra optimization (increases build times).",
            flag=True,
        ),
        option(
            "node-cache",
            description=(
                "Per-recipe cache policy: RECIPE_GLOB={auto,ignore,reuse}; "
                "accepts shell "
                "wildcards, and is repeatable and comma-separated."
            ),
            flag=False,
            multiple=True,
        ),
        option(
            "enable-sccache",
            description="Enable sccache for intermediate object files.",
            flag=True,
        ),
    ]

    _loggers: ClassVar = ["ggbuild.build"]

    def _generic(self) -> bool:
        return bool(self.option("generic"))

    def _options(self) -> _BuildOptions:
        package = self.argument("name")
        if not package:
            raise ValueError(
                "a recipe argument is required unless --target is supplied"
            )
        tags_string = self.option("pkg-tags")
        compression_string = self.option("pkg-compression")
        tags = {}
        if tags_string:
            for pair in tags_string.split(","):
                key, _, value = pair.strip().partition("=")
                tags[key.strip()] = value.strip()
        return _BuildOptions(
            package=package,
            destination=self.option("dest") or "",
            work_dir=self.option("work-dir"),
            keepwork=self.option("keepwork"),
            clean=self.option("clean"),
            generic=self._generic(),
            arch=self.option("arch"),
            libc=self.option("libc"),
            build_source=self.option("build-source"),
            build_dbgsym=self.option("build-dbgsym"),
            version=self.option("source-ref"),
            revision=self.option("pkg-revision"),
            subdist=self.option("pkg-subdist"),
            is_release=self.option("release"),
            extra_opt=self.option("extra-optimizations"),
            jobs=self.option("jobs") or 0,
            tags=tags,
            compression=compression_string.split(",")
            if compression_string
            else [],
            enable_sccache=bool(self.option("enable-sccache")),
        )

    def _root_package(
        self, options: _BuildOptions, target: targets.Target
    ) -> mpkg_base.BundledPackage:
        modname, _, clsname = options.package.rpartition(":")
        package_class = getattr(importlib.import_module(modname), clsname)
        if not isinstance(package_class, type) or not issubclass(
            package_class, mpkg_base.BundledPackage
        ):
            raise TypeError(f"{options.package} is not a bundled package class")
        registered = (
            package_class.registered_release(options.version)
            if options.version is not None
            else None
        )
        if registered is not None:
            package = registered.clone()
            if options.tags:
                package.set_metadata_tags(options.tags)
            return package
        package = package_class.resolve(
            self.io,
            version=options.version,
            revision=options.revision,
            is_release=options.is_release,
            target=target,
        )
        if options.tags:
            package.set_metadata_tags(options.tags)
        return package

    def _consistent_dependencies(
        self,
        env: poetry_env.Env,
        target: targets.Target,
        package: mpkg_base.BundledPackage,
    ) -> tuple[list[mpkg_base.BasePackage], list[mpkg_base.BasePackage]] | None:
        packages, build_packages = self._resolve_deps(env, target, package, [])
        mismatches = self._check_dep_consistency(packages, build_packages)
        if mismatches:
            packages, build_packages = self._resolve_deps(
                env, target, package, mismatches
            )
        mismatches = self._check_dep_consistency(packages, build_packages)
        if not mismatches:
            return packages, build_packages
        self.io.write_error_line(
            "Unresolveable install-time vs build-time dependency graph. "
            "Mismatching dependencies: "
            + ", ".join(dep.to_pep_508() for dep in mismatches)
        )
        return None

    def handle(self) -> int:
        if target_name := self.option("target"):
            return self._handle_project(target_name)
        options = self._options()
        if options.clean and not options.keepwork:
            raise ValueError("--clean requires --keepwork")
        if self.option("node-cache") and not options.keepwork:
            raise ValueError("--node-cache requires --keepwork")

        # Older yum, fakeroot and possibly other tools
        # customarily attempt to iterate over _all_
        # file descriptors up to the limit and close them
        # when forking subprocesses, which in the case of
        # a high limit may be prohibitively expensive,
        # so clamp RLIMIT_NOFILE to a lower value.
        self._clamp_rlimit_nofile()

        target = targets.detect_target(
            self.io,
            portable=options.generic,
            libc=options.libc,
            arch=options.arch,
        )
        target.prepare()
        root_pkg = self._root_package(options, target)
        env = poetry_env.SystemEnv(pathlib.Path(sys.executable))
        dependencies = self._consistent_dependencies(env, target, root_pkg)
        if dependencies is None:
            return 1
        packages, build_packages = dependencies

        work_context: contextlib.AbstractContextManager[str]
        if options.work_dir:
            workdir_path = pathlib.Path(options.work_dir)
            workdir_path.mkdir(parents=True, exist_ok=True)
            workdir = str(workdir_path)
            work_context = contextlib.nullcontext(workdir)
        elif options.keepwork:
            identity = "-".join((
                options.package,
                root_pkg.name_slot,
                str(root_pkg.version),
                target.triple,
                options.revision or "1",
            ))
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", identity).strip("-.")
            digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
            workdir_path = (
                ggbuild_cache.cachedir() / "work" / f"{slug}-{digest}"
            )
            workdir_path.mkdir(parents=True, exist_ok=True)
            workdir = str(workdir_path)
            self.io.write_line(f"<info>Work directory: {workdir}</info>")
            work_context = contextlib.nullcontext(workdir)
        else:
            work_context = tempfile.TemporaryDirectory(prefix="ggbuild.")
        subject = f"{root_pkg} for {target.triple}"
        with (
            work_context as workdir,
            stage(
                "build",
                process="building",
                subject=subject,
                sticky=True,
            ),
        ):
            extra = (
                {"compression": options.compression}
                if options.compression
                else {}
            )
            target.build(
                targets.BuildRequest(
                    io=self.io,
                    env=env,
                    root_pkg=root_pkg,
                    deps=packages,
                    build_deps=build_packages,
                    workdir=workdir,
                    keepwork=options.keepwork,
                    outputdir=options.destination,
                    build_source=options.build_source,
                    build_dbgsym=options.build_dbgsym,
                    build_date=datetime.datetime.now(tz=datetime.UTC),
                    revision=options.revision or "1",
                    subdist=options.subdist,
                    extra_opt=options.extra_opt,
                    enable_sccache=options.enable_sccache,
                    jobs=options.jobs,
                    prebuilt_packages=tuple(
                        json.loads(
                            os.environ.get("GGBUILD_PREBUILT_PACKAGES", "[]")
                        )
                    ),
                    node_staging=(
                        pathlib.Path(path)
                        if (path := os.environ.get("GGBUILD_NODE_STAGING"))
                        else None
                    ),
                    bundle_install_subdir=(
                        pathlib.Path(path)
                        if (
                            path := os.environ.get(
                                "GGBUILD_BUNDLE_INSTALL_SUBDIR"
                            )
                        )
                        else None
                    ),
                    node_cache=self._node_cache_policy(clean=options.clean),
                    **extra,
                ),
            )

        return 0

    def _handle_project(self, target_name: str) -> int:
        if self.argument("name"):
            raise ValueError(
                "the recipe argument and --target cannot be used together"
            )
        config = load_project()
        config.target(target_name)
        source_ref = self.option("source-ref")
        options = RunOptions(
            targets=(target_name,),
            versions=(source_ref,) if source_ref else (),
            cache_dir=config.root / ".cache",
            destination=pathlib.Path(
                self.option("dest") or config.root / "dist"
            ),
            max_parallel=int(
                self.option("jobs") or config.workflow.max_concurrency
            ),
            node_cache=self._node_cache_values(clean=self.option("clean")),
            enable_sccache=(
                bool(self.option("enable-sccache"))
                or config.sccache.pull_request
            ),
        )
        summary = run_local(options, config=config)
        self.io.write_line(format_summary(summary))
        return 0

    def _node_cache_values(self, *, clean: bool) -> tuple[str, ...]:
        values = tuple(self.option("node-cache") or ())
        if clean:
            values = ("*=ignore", *values)
        return values

    def _node_cache_policy(self, *, clean: bool) -> dict[str, str]:
        return parse_node_cache(self._node_cache_values(clean=clean))

    def _resolve_deps(
        self,
        env: poetry_env.Env,
        target: targets.Target,
        root_pkg: mpkg_base.BundledPackage,
        extra_deps: list[poetry_dep.Dependency] | None = None,
    ) -> tuple[list[mpkg_base.BasePackage], list[mpkg_base.BasePackage]]:
        root = self._solver_root("__root__", root_pkg, extra_deps or [])
        af_repo.bundle_repo.add_package(root)
        extras = [
            *(f"capability-{cap}" for cap in target.get_capabilities()),
            *root_pkg.features,
        ]
        repo_pool = af_repo.Pool()
        repo_pool.add_repository(target.get_package_repository())
        repo_pool.add_repository(
            af_repo.bundle_repo,
            priority=poetry_repository_pool.Priority.SUPPLEMENTAL,
        )

        item_repo = root_pkg.get_package_repository(target, io=self.io)
        if item_repo is not af_repo.bundle_repo:
            repo_pool.add_repository(
                item_repo,
                priority=poetry_repository_pool.Priority.SUPPLEMENTAL,
            )

        try:
            packages = self._solve_runtime(
                env,
                root,
                repo_pool,
                extras,
                subject=str(root_pkg),
            )
        finally:
            af_repo.bundle_repo.remove_package(root)
        build_root = self._solver_root("__build_root__", root_pkg, [])
        mpkg_base.all_requires_include_build_reqs = True
        try:
            resolution = self._solve(
                build_root,
                repo_pool,
                extras,
                include_build_reqs=True,
                subject=str(root_pkg),
            )
            package_map, graph = self._build_dependency_graph(env, resolution)
        finally:
            mpkg_base.all_requires_include_build_reqs = False
        return packages, self._sort_build_dependencies(package_map, graph)

    @staticmethod
    def _solver_root(
        name: str,
        root_pkg: mpkg_base.BundledPackage,
        extra_deps: list[poetry_dep.Dependency],
    ) -> project_package.ProjectPackage:
        root = project_package.ProjectPackage(name, "1")
        root.python_versions = (
            af_python.get_python_runtime_dependency().pretty_constraint
        )
        root.add_dependency(
            poetry_dep.Dependency(root_pkg.name, root_pkg.version)
        )
        for dependency in extra_deps:
            root.add_dependency(dependency)
        return root

    def _solve(
        self,
        root: project_package.ProjectPackage,
        repo_pool: af_repo.Pool,
        extras: list[str],
        *,
        include_build_reqs: bool = False,
        subject: str,
    ) -> list[mpkg_base.BasePackage]:
        provider = af_repo.Provider(
            root,
            repo_pool,
            self.io,
            include_build_reqs=include_build_reqs,
            extras=extras,
        )
        solver = _Solver(root, repo_pool, [], [], self.io)
        solver.__dict__["_provider"] = provider
        dependency_kind = "build" if include_build_reqs else "runtime"
        with stage(
            f"resolve/{dependency_kind}",
            process="resolve",
            subject=f"{dependency_kind} dependencies for {subject}",
        ):
            return [
                cast("mpkg_base.BasePackage", package)
                for package in solver.solve().get_solved_packages()
            ]

    def _solve_runtime(
        self,
        env: poetry_env.Env,
        root: project_package.ProjectPackage,
        repo_pool: af_repo.Pool,
        extras: list[str],
        *,
        subject: str,
    ) -> list[mpkg_base.BasePackage]:
        resolution = self._solve(root, repo_pool, extras, subject=subject)
        package_map = {package.name: package for package in resolution}
        graph = {
            package.name: {
                requirement.name
                for requirement in package.requires
                if env.is_valid_for_marker(requirement.marker)
            }
            for package in resolution
        }
        return [
            package_map[name]
            for name in graphlib.TopologicalSorter(graph).static_order()
        ]

    @staticmethod
    def _build_dependency_graph(
        env: poetry_env.Env,
        resolution: list[mpkg_base.BasePackage],
    ) -> tuple[
        dict[mpkg_base.NormalizedName, mpkg_base.BasePackage],
        dict[mpkg_base.NormalizedName, set[mpkg_base.NormalizedName]],
    ]:
        package_map = {package.name: package for package in resolution}
        graph = {
            package.name: {
                requirement.name
                for requirement in set(package.requires)
                | set(mpkg_base.get_build_requirements(package))
                if requirement.is_activated()
                and env.is_valid_for_marker(requirement.marker)
                and requirement.name != package.name
            }
            for package in resolution
        }
        return package_map, graph

    @staticmethod
    def _sort_build_dependencies(
        package_map: dict[mpkg_base.NormalizedName, mpkg_base.BasePackage],
        graph: dict[mpkg_base.NormalizedName, set[mpkg_base.NormalizedName]],
    ) -> list[mpkg_base.BasePackage]:
        # Workaround cycles in build/runtime dependencies between
        # packages.  This requires the depending package to explicitly
        # declare its cyclic runtime dependencies in get_cyclic_runtime_deps()
        # and then the cyclic dependency must take care to inject itself
        # into the dependent's context to build itself (e.g. by manipulating
        # PYTHONPATH at build time.)  An example of such cycle is
        # flit-core -> tomli -> flit-core.
        cyclic_runtime_deps = collections.defaultdict(list)
        last_cycle = None
        current_cycle = None
        while True:
            sorter = graphlib.TopologicalSorter({
                name: tuple(sorted(graph[name])) for name in sorted(graph)
            })

            try:
                build_pkgs = [package_map[pn] for pn in sorter.static_order()]
            except graphlib.CycleError as e:
                cycle = e.args[1]
                if len(cycle) > 3 or cycle == last_cycle:
                    raise

                dep = package_map[cycle[-1]]
                pkg_with_dep = package_map[cycle[-2]]
                if (
                    isinstance(pkg_with_dep, af_python.PythonPackage)
                    and dep.name not in pkg_with_dep.get_cyclic_runtime_deps()
                ):
                    dep, pkg_with_dep = pkg_with_dep, dep
                    if (
                        isinstance(pkg_with_dep, af_python.PythonPackage)
                        and dep.name
                        not in pkg_with_dep.get_cyclic_runtime_deps()
                    ):
                        raise

                last_cycle = current_cycle
                current_cycle = cycle
                cyclic_runtime_deps[pkg_with_dep].append(dep)
                graph[pkg_with_dep.name].remove(dep.name)
            else:
                break

        for pkg_with_cr_deps, cr_deps in cyclic_runtime_deps.items():
            for i, build_pkg in enumerate(build_pkgs):
                if build_pkg == pkg_with_cr_deps:
                    build_pkgs[i + 1 : i + 1] = cr_deps
                    break

        return build_pkgs

    def _check_dep_consistency(
        self,
        packages: list[mpkg_base.BasePackage],
        build_pkgs: list[mpkg_base.BasePackage],
    ) -> list[poetry_dep.Dependency]:
        build_dep_index = {pkg.name: pkg for pkg in build_pkgs}
        reresolve_deps = []
        for pkg in packages:
            build_dep = build_dep_index.get(pkg.name)
            if build_dep is not None and build_dep.version != pkg.version:
                reresolve_deps.append(
                    poetry_dep.Dependency(build_dep.name, build_dep.version)
                )

        return reresolve_deps

    def _clamp_rlimit_nofile(self) -> None:
        if sys.platform == "win32":
            return

        resource_spec = importlib.util.find_spec("resource")
        if resource_spec is not None:
            resource = importlib.import_module("resource")
            try:
                fno_soft, fno_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            except OSError:
                self.io.write_error_line("could not read RLIMIT_NOFILE")
            else:
                if fno_soft > 8192 or fno_hard > 8192:
                    try:
                        resource.setrlimit(
                            resource.RLIMIT_NOFILE,
                            (min(8192, fno_soft), min(8192, fno_hard)),
                        )
                    except OSError:
                        self.io.write_error_line("could not read RLIMIT_NOFILE")
