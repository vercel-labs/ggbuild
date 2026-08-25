# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

"""Target validation and host/container launching for project builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import dataclasses
import importlib.util
import io
import json
import os
import pathlib
import platform
import re
import subprocess
import sys
import tarfile
import tempfile

import docker
from docker.errors import BuildError, ContainerError
from lograil import FileDescriptorLogSource, status, tail_to_status
from lograil.sources.docker import DockerBuildLogSource, DockerLogSource

from ggbuild.project import ProjectConfig, TargetConfig
from ggbuild.targets.linux.dockerfile import docker_environment

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from docker.models.containers import Container

    from ggbuild.ci_protocol import ExecutionPaths

_CONTAINER_TMP = str(pathlib.PurePosixPath("/") / "tmp")
_CONTAINER_LOCALE = "C.UTF-8"


def _container_tmp_directory(
    node: Mapping[str, object],
    container_work: pathlib.PurePosixPath,
    host_work: pathlib.Path,
) -> str:
    if node["role"] == "test":
        return _CONTAINER_TMP
    (host_work / "tmp").mkdir(parents=True, exist_ok=True)
    return str(container_work / "tmp")


@dataclasses.dataclass(frozen=True, slots=True)
class Target:
    triple: str
    arch: str
    os: str
    libc: str | None


def normalize_arch(value: str) -> str:
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(
        value.lower(), value.lower()
    )


def normalize_system(value: str) -> str:
    return value.lower()


def parse_target(triple: str) -> Target:
    arch = normalize_arch(triple.split("-", maxsplit=1)[0])
    if triple.endswith("-unknown-linux-gnu"):
        return Target(triple, arch, "linux", "gnu")
    if triple.endswith("-unknown-linux-musl"):
        return Target(triple, arch, "linux", "musl")
    if triple.endswith("-apple-darwin"):
        return Target(triple, arch, "darwin", None)
    if "-windows-" in triple:
        return Target(triple, arch, "windows", None)
    raise ValueError(f"unsupported target triple: {triple}")


def detect_host_target(
    *,
    host_system: str | None = None,
    host_arch: str | None = None,
    host_libc: str | None = None,
) -> Target:
    system = normalize_system(host_system or platform.system())
    arch = normalize_arch(host_arch or platform.machine())
    if system == "darwin":
        return parse_target(f"{arch}-apple-darwin")
    if system == "windows":
        return parse_target(f"{arch}-pc-windows-msvc")
    if system == "linux":
        libc_name = host_libc
        if libc_name is None:
            libc_name, _ = platform.libc_ver()
        libc = {"glibc": "gnu", "gnu": "gnu", "musl": "musl"}.get(
            libc_name.lower()
        )
        if libc is None:
            raise ValueError(
                f"unsupported host C library: {libc_name or 'unknown'}"
            )
        return parse_target(f"{arch}-unknown-linux-{libc}")
    raise ValueError(f"unsupported host system: {system}")


def validate_execution(
    target_config: TargetConfig,
    *,
    host_system: str | None = None,
    host_arch: str | None = None,
) -> None:
    target = parse_target(target_config.triple)
    system = normalize_system(host_system or platform.system())
    arch = normalize_arch(host_arch or platform.machine())
    if target_config.execution == "host" and arch != target.arch:
        raise ValueError(
            f"target {target.arch} requires matching host hardware, got {arch}"
        )
    if target_config.execution == "host" and system != target.os:
        raise ValueError(
            f"cannot build {target.os} target on {system} with host execution"
        )
    if target_config.execution == "docker" and target.os != "linux":
        raise ValueError("Docker execution is only supported for Linux targets")


def validate_prepared_target(
    target_config: TargetConfig,
    *,
    host_system: str | None = None,
    host_arch: str | None = None,
    host_libc: str | None = None,
) -> None:
    """Validate a target supplied by the current execution environment."""
    if target_config.execution != "docker":
        raise ValueError(
            f"prepared target {target_config.triple} is not a Docker target"
        )
    host = detect_host_target(
        host_system=host_system,
        host_arch=host_arch,
        host_libc=host_libc,
    )
    if host.triple != target_config.triple:
        raise ValueError(
            f"prepared target {target_config.triple} requires an exact host "
            f"match, got {host.triple}"
        )


def package_mount(module_name: str) -> tuple[pathlib.Path, str]:
    package_name = module_name.partition(".")[0]
    spec = importlib.util.find_spec(package_name)
    if spec is None:
        raise ValueError(f"cannot find package {package_name!r}")
    locations = spec.submodule_search_locations
    if locations is not None:
        paths = list(locations)
        if len(paths) != 1:
            raise ValueError(f"package {package_name!r} must have one location")
        return pathlib.Path(paths[0]), f"/opt/ggbuild-modules/{package_name}"
    if spec.origin is None:
        raise ValueError(f"module {package_name!r} has no filesystem path")
    return pathlib.Path(spec.origin), f"/opt/ggbuild-modules/{package_name}.py"


def low_level_build_args(
    node: Mapping[str, object],
    config: ProjectConfig,
    *,
    destination: str,
    build_work_directory: str | None = None,
    artifact_directory: str | None = None,
    test_work_directory: str | None = None,
    bare_linux_test: bool = False,
    enable_sccache: bool = False,
) -> list[str]:
    if node["role"] == "test":
        if artifact_directory is None:
            raise ValueError("test nodes require an artifact directory")
        test_work = test_work_directory or str(
            pathlib.PurePosixPath(destination).parent / "test-work"
        )
        arguments = [
            "test",
            artifact_directory,
            f"--recipe={node['recipe']}",
            f"--work-dir={test_work}",
            f"--result={destination}/test-result.json",
        ]
        if bare_linux_test:
            arguments.append("--bare-linux")
        return arguments
    target = parse_target(str(node["target"]))
    raw_options = node["inputs"]
    if not isinstance(raw_options, dict) or not isinstance(
        raw_options.get("build_options"), dict
    ):
        raise TypeError("node build options are invalid")
    options = cast("dict[str, Any]", raw_options)
    build_options = cast("dict[str, Any]", options["build_options"])
    args = [
        "build",
        f"--dest={destination}",
        f"--arch={target.arch}",
        f"--source-ref={node['version']}",
        *(
            [f"--work-dir={build_work_directory}"]
            if build_work_directory is not None
            else []
        ),
    ]
    if config.portable:
        args.append("--generic")
    if enable_sccache:
        args.append("--enable-sccache")
    if target.libc is not None:
        args.append(f"--libc={target.libc}")
    if build_options["build_dbgsym"]:
        args.append("--build-dbgsym")
    if build_options["build_source"]:
        args.append("--build-source")
    if build_options["release"]:
        args.append("--release")
    if build_options["extra_optimizations"]:
        args.append("--extra-optimizations")
    args.append(f"--pkg-revision={build_options['revision']}")
    compression = build_options["compression"]
    if compression:
        args.append(f"--pkg-compression={','.join(compression)}")
    args.append(str(node["recipe"]))
    return args


def docker_image_name(config: ProjectConfig, target_name: str) -> str:
    target = config.target(target_name)
    if target.execution != "docker":
        raise ValueError(f"target {target_name} is not a Docker target")
    container = docker_environment(target_name)
    project = re.sub(r"[^a-z0-9_.-]+", "-", config.project_name.lower())
    arch = parse_target(target_name).arch
    return f"ggbuild/{project}:{container.name}-{arch}"


def docker_image_build_command(
    config: ProjectConfig,
    target_name: str,
    *,
    context: pathlib.Path | None = None,
) -> list[str]:
    target = config.target(target_name)
    if target.execution != "docker":
        raise ValueError(f"target {target_name} is not a Docker target")
    container = docker_environment(target_name)
    return [
        "docker",
        "build",
        "--progress=plain",
        "--platform",
        container.platform,
        "--tag",
        docker_image_name(config, target_name),
        "--file",
        "-",
        str(context or config.root),
    ]


def docker_run_command(
    node: Mapping[str, object],
    paths: ExecutionPaths,
    config: ProjectConfig,
    *,
    raw_output: pathlib.Path,
    environment: Mapping[str, str],
) -> list[str]:
    target_name = str(node["target"])
    target = config.target(target_name)
    if target.execution != "docker":
        raise ValueError(f"target {target_name} is not a Docker target")
    container = docker_environment(target_name)
    recipe_module = str(node["recipe"]).rpartition(":")[0]
    mounts = {
        recipe_module.partition(".")[0]: package_mount(recipe_module),
        "ggbuild": package_mount("ggbuild"),
        "lograil": package_mount("lograil"),
    }
    staging = pathlib.Path(environment["GGBUILD_NODE_STAGING"]).resolve()
    container_cache = pathlib.Path(
        environment.get(
            "GGBUILD_CONTAINER_CACHE", str(paths.bundle_dir / "container-cache")
        )
    ).resolve()
    container_work = pathlib.PurePosixPath("/ggbuild-node")
    container_staging = str(container_work / staging.name)
    container_tmp = _container_tmp_directory(
        node, container_work, staging.parent
    )
    user = _host_container_user()
    passwd, group = _container_identity_files(
        paths.bundle_dir / "container-identity", user
    )
    (staging.parent / "home").mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--platform",
        container.platform,
        "--user",
        user,
    ]
    for name in (
        "ACTIONS_CACHE_SERVICE_V2",
        "ACTIONS_RESULTS_URL",
        "ACTIONS_RUNTIME_TOKEN",
        "GGBUILD_ENABLE_SCCACHE",
        "SCCACHE_GHA_ENABLED",
    ):
        if name in environment:
            command.extend(("--env", name))
    for _, (source, destination) in sorted(mounts.items()):
        command.extend(("--volume", f"{source}:{destination}:ro"))
    command.extend((
        "--volume",
        f"{raw_output}:/artifacts",
        "--volume",
        f"{paths.output_dir.resolve()}:/produced:ro",
        "--volume",
        f"{container_cache}:/ggbuild-cache",
        "--volume",
        f"{staging.parent}:{container_work}",
        "--volume",
        f"{passwd}:/etc/passwd:ro",
        "--volume",
        f"{group}:/etc/group:ro",
        "--workdir",
        "/opt/ggbuild-modules",
        "--env",
        "PYTHONPATH=/opt/ggbuild-modules",
        "--env",
        "LOGRAIL_OUTPUT=json",
        "--env",
        f"TMPDIR={container_tmp}",
        "--env",
        f"LC_ALL={_CONTAINER_LOCALE}",
        "--env",
        f"HOME={container_work / 'home'}",
        "--env",
        "XDG_CACHE_HOME=/ggbuild-cache",
        "--env",
        f"GGBUILD_NODE_STAGING={container_staging}",
        "--env",
        f"GGBUILD_PREBUILT_PACKAGES={environment['GGBUILD_PREBUILT_PACKAGES']}",
        "--env",
        f"GGBUILD_BUNDLE_INSTALL_SUBDIR={environment['GGBUILD_BUNDLE_INSTALL_SUBDIR']}",
        "--entrypoint",
        "/opt/venv/bin/python",
        docker_image_name(config, target_name),
        "-m",
        "ggbuild",
        *low_level_build_args(
            node,
            config,
            destination="/artifacts",
            build_work_directory=str(container_work / "build"),
            artifact_directory=(
                f"/produced/{node['target']}/{node['version']}"
            ),
            test_work_directory=f"{container_work}/test-work",
            enable_sccache=environment.get("GGBUILD_ENABLE_SCCACHE") == "1",
        ),
    ))
    return command


def _docker_build_context(dockerfile: str) -> io.BytesIO:
    context = io.BytesIO()
    contents = dockerfile.encode()
    with tarfile.open(fileobj=context, mode="w") as archive:
        info = tarfile.TarInfo("Dockerfile")
        info.size = len(contents)
        archive.addfile(info, io.BytesIO(contents))
    context.seek(0)
    return context


def _docker_build_lines(
    records: Iterable[dict[str, Any]],
    *,
    errors: list[str],
) -> Iterator[str]:
    for record in records:
        error = record.get("error")
        if isinstance(error, str):
            errors.append(error)
        stream = record.get("stream")
        if isinstance(stream, str):
            yield from stream.splitlines(keepends=True)
            continue
        status_message = record.get("status")
        if not isinstance(status_message, str):
            continue
        identifier = record.get("id")
        progress = record.get("progress")
        parts = [
            value
            for value in (identifier, status_message, progress)
            if isinstance(value, str) and value
        ]
        if parts:
            yield " ".join(parts) + "\n"


def _host_container_user() -> str:
    if not hasattr(os, "getuid"):
        return "65534:65534"
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        return "65534:65534"
    return f"{uid}:{gid}"


def docker_container_user(client: docker.DockerClient) -> str:
    del client
    return _host_container_user()


def _container_identity_files(
    directory: pathlib.Path, user: str
) -> tuple[pathlib.Path, pathlib.Path]:
    uid, gid = user.split(":", 1)
    if not uid.isdecimal() or not gid.isdecimal():
        raise ValueError(f"invalid container user {user!r}")
    directory.mkdir(parents=True, exist_ok=True)
    passwd = directory / f"passwd-{uid}-{gid}"
    group = directory / f"group-{uid}-{gid}"
    _write_atomic(
        passwd,
        "root:x:0:0:root:/root:/bin/sh\n"
        f"ggbuild:x:{uid}:{gid}:ggbuild:/ggbuild-node/home:/bin/sh\n",
    )
    _write_atomic(group, f"root:x:0:\nggbuild:x:{gid}:\n")
    return passwd, group


def _write_atomic(path: pathlib.Path, content: str) -> None:
    temporary: pathlib.Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary = pathlib.Path(output.name)
            output.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_docker_build(
    *,
    dockerfile: str,
    environment: Mapping[str, str],
    image: str,
    platform: str,
) -> None:
    client = docker.DockerClient.from_env(environment=environment)
    errors: list[str] = []
    build_log: list[dict[str, str]] = []
    context = _docker_build_context(dockerfile)
    try:
        records = client.api.build(
            fileobj=context,
            custom_context=True,
            dockerfile="Dockerfile",
            tag=image,
            platform=platform,
            rm=True,
            decode=True,
        )
        lines = _docker_build_lines(records, errors=errors)
        source = DockerBuildLogSource(lines, image_name=image)
        with (
            status(process="build", subject=image, sticky=True),
            tail_to_status(source=source, delay=0, lossy=False) as drained,
        ):
            drained.wait()
            if drained.error is not None:
                raise RuntimeError(
                    "failed to read Docker build output"
                ) from drained.error
        if errors:
            build_log.extend({"error": message} for message in errors)
            raise BuildError(errors[-1], iter(build_log))
    finally:
        try:
            context.close()
        finally:
            client.close()


def _docker_container_options(
    node: Mapping[str, object],
    paths: ExecutionPaths,
    config: ProjectConfig,
    *,
    raw_output: pathlib.Path,
    environment: Mapping[str, str],
    user: str | None = None,
) -> dict[str, Any]:
    target_name = str(node["target"])
    target = config.target(target_name)
    if target.execution != "docker":
        raise ValueError(f"target {target_name} is not a Docker target")
    container_environment = docker_environment(target_name)
    recipe_module = str(node["recipe"]).rpartition(":")[0]
    package_mounts = {
        recipe_module.partition(".")[0]: package_mount(recipe_module),
        "ggbuild": package_mount("ggbuild"),
        "lograil": package_mount("lograil"),
    }
    volumes = {
        str(source): {"bind": destination, "mode": "ro"}
        for _, (source, destination) in sorted(package_mounts.items())
    }
    staging = pathlib.Path(environment["GGBUILD_NODE_STAGING"]).resolve()
    container_cache = pathlib.Path(
        environment.get(
            "GGBUILD_CONTAINER_CACHE", str(paths.bundle_dir / "container-cache")
        )
    ).resolve()
    container_work = pathlib.PurePosixPath("/ggbuild-node")
    (staging.parent / "home").mkdir(parents=True, exist_ok=True)
    container_tmp = _container_tmp_directory(
        node, container_work, staging.parent
    )
    cache_destination = "/ggbuild-cache" if user is not None else "/root/.cache"
    volumes.update({
        str(raw_output): {"bind": "/artifacts", "mode": "rw"},
        str(paths.output_dir.resolve()): {
            "bind": "/produced",
            "mode": "ro",
        },
        str(container_cache): {"bind": cache_destination, "mode": "rw"},
        str(staging.parent): {
            "bind": str(container_work),
            "mode": "rw",
        },
    })
    if user is not None:
        passwd, group = _container_identity_files(
            paths.bundle_dir / "container-identity", user
        )
        volumes.update({
            str(passwd): {"bind": "/etc/passwd", "mode": "ro"},
            str(group): {"bind": "/etc/group", "mode": "ro"},
        })
    container_options: dict[str, Any] = {
        "image": docker_image_name(config, target_name),
        "command": [
            "-m",
            "ggbuild",
            *low_level_build_args(
                node,
                config,
                destination="/artifacts",
                build_work_directory=str(container_work / "build"),
                artifact_directory=(
                    f"/produced/{node['target']}/{node['version']}"
                ),
                test_work_directory=f"{container_work}/test-work",
                enable_sccache=(
                    environment.get("GGBUILD_ENABLE_SCCACHE") == "1"
                ),
            ),
        ],
        "detach": True,
        "entrypoint": "/opt/venv/bin/python",
        "environment": {
            "PYTHONPATH": "/opt/ggbuild-modules",
            "LOGRAIL_OUTPUT": "plain",
            "GGBUILD_NODE_STAGING": str(container_work / staging.name),
            "GGBUILD_PREBUILT_PACKAGES": environment[
                "GGBUILD_PREBUILT_PACKAGES"
            ],
            "GGBUILD_BUNDLE_INSTALL_SUBDIR": environment[
                "GGBUILD_BUNDLE_INSTALL_SUBDIR"
            ],
            "TMPDIR": container_tmp,
            "LC_ALL": _CONTAINER_LOCALE,
        },
        "init": True,
        "platform": container_environment.platform,
        "volumes": volumes,
        "working_dir": "/opt/ggbuild-modules",
    }
    if user is not None:
        container_options["user"] = user
        container_environment_variables = container_options["environment"]
        container_environment_variables["HOME"] = str(container_work / "home")
        container_environment_variables["XDG_CACHE_HOME"] = cache_destination
    container_environment_variables = container_options["environment"]
    for name in (
        "ACTIONS_CACHE_SERVICE_V2",
        "ACTIONS_RESULTS_URL",
        "ACTIONS_RUNTIME_TOKEN",
        "GGBUILD_ENABLE_SCCACHE",
        "SCCACHE_GHA_ENABLED",
    ):
        if name in environment:
            container_environment_variables[name] = environment[name]
    return container_options


def run_docker_container(
    node: Mapping[str, object],
    paths: ExecutionPaths,
    config: ProjectConfig,
    *,
    raw_output: pathlib.Path,
    environment: Mapping[str, str],
    subject: str,
) -> None:
    client = docker.DockerClient.from_env(environment=environment)
    options = _docker_container_options(
        node,
        paths,
        config,
        raw_output=raw_output,
        environment=environment,
        user=docker_container_user(client),
    )
    container: Container | None = None
    try:
        container = client.containers.create(**options)
        container.start()
        logs = container.logs(
            stream=True,
            follow=True,
            stdout=True,
            stderr=True,
        )
        source = DockerLogSource(logs)
        with (
            status(process="build", subject=subject, sticky=True),
            tail_to_status(source=source, delay=0, lossy=False) as drained,
        ):
            result = container.wait()
            drained.wait()
            if drained.error is not None:
                raise RuntimeError(
                    "failed to read Docker container output"
                ) from drained.error
        returncode = result["StatusCode"]
        if returncode:
            raise ContainerError(
                container,
                returncode,
                options["command"],
                options["image"],
                None,
            )
    finally:
        try:
            if container is not None:
                container.remove(force=True)
        finally:
            client.close()


def run_child_build(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    subject: str,
    structured: bool,
) -> None:
    with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
        command,
        cwd=pathlib.Path.cwd(),
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    ) as process:
        if process.stdout is None:
            raise RuntimeError("failed to capture child build output")
        if structured:
            source = FileDescriptorLogSource(
                process.stdout.fileno(), name="ggbuild"
            )
            with (
                status(process="build", subject=subject, sticky=True),
                tail_to_status(source=source, delay=0, lossy=False) as drained,
            ):
                returncode = process.wait()
                drained.wait()
                if drained.error is not None:
                    raise RuntimeError(
                        "failed to read child build output"
                    ) from drained.error
        else:
            for line in process.stdout:
                sys.stderr.write(line)
            returncode = process.wait()
        if returncode:
            raise subprocess.CalledProcessError(returncode, command)


def build_command(
    node: Mapping[str, object],
    paths: ExecutionPaths,
    config: ProjectConfig,
    *,
    environment: Mapping[str, str],
    prepared_target: str | None = None,
    bare_linux_test: bool = False,
) -> list[str]:
    target_name = str(node["target"])
    target = config.target(target_name)
    if prepared_target is not None and target_name != prepared_target:
        raise ValueError(
            f"node target {target_name} does not match prepared target "
            f"{prepared_target}"
        )
    if (
        target.execution == "docker"
        and prepared_target is None
        and not (node["role"] == "test" and bare_linux_test)
    ):
        return docker_run_command(
            node,
            paths,
            config,
            raw_output=paths.work_dir / "raw-output",
            environment=environment,
        )
    return [
        sys.executable,
        "-m",
        "ggbuild",
        *low_level_build_args(
            node,
            config,
            destination=str(paths.work_dir / "raw-output"),
            build_work_directory=str(paths.work_dir / "build"),
            artifact_directory=str(
                paths.output_dir / str(node["target"]) / str(node["version"])
            ),
            bare_linux_test=bare_linux_test,
            enable_sccache=environment.get("GGBUILD_ENABLE_SCCACHE") == "1",
        ),
    ]


def prepare_target(config: ProjectConfig, target_name: str) -> None:
    target = config.target(target_name)
    validate_execution(target)
    if target.execution != "docker":
        return
    image = docker_image_name(config, target_name)
    environment = os.environ.copy()
    if environment.get("GGBUILD_IMAGE_PREPARED") == image:
        return
    container = docker_environment(target_name)
    run_docker_build(
        dockerfile=container.render_dockerfile(),
        environment=environment,
        image=image,
        platform=container.platform,
    )


def node_environment(
    node: Mapping[str, object], paths: ExecutionPaths, config: ProjectConfig
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(config.target(str(node["target"])).environment_dict)
    environment["GGBUILD_BUNDLE_INSTALL_SUBDIR"] = str(
        node["bundle_install_subdir"]
    )
    manifest = paths.work_dir / "dependency-nodes.json"
    dependencies: list[dict[str, object]] = []
    if manifest.is_file():
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            dependencies = loaded
    environment["GGBUILD_PREBUILT_PACKAGES"] = json.dumps(
        sorted(str(dependency["package"]) for dependency in dependencies)
    )
    environment["GGBUILD_NODE_STAGING"] = str(paths.work_dir / "sysroot")
    environment.setdefault(
        "GGBUILD_CONTAINER_CACHE", str(paths.bundle_dir / "container-cache")
    )
    return environment
