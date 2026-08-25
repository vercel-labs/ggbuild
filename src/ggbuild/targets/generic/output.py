# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import TYPE_CHECKING

import ntpath
import pathlib
import posixpath
import re
import shlex

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lograil import LogEntry

type LexicalPath = pathlib.PurePosixPath | pathlib.PureWindowsPath


_COMPILER_RE = re.compile(
    r"(?:.*-)?(?:cc|gcc|clang|c\+\+|g\+\+|clang\+\+)(?:-\d+(?:\.\d+)*)?"
)
_MAKE_DIRECTORY_RE = re.compile(
    r"^(?:g?make)(?:\[(?P<level>\d+)\])?: "
    r"(?P<action>Entering|Leaving) directory "
    r"[\u2018'`](?P<path>.+?)[\u2019']$"
)
_LINK_RE = re.compile(
    r"^(?P<label>link)\s+(?P<source>.+?)\s+->\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_C_SOURCE_SUFFIXES = frozenset({".c", ".m"})
_CXX_SOURCE_SUFFIXES = frozenset({
    ".C",
    ".cc",
    ".cp",
    ".cpp",
    ".cxx",
    ".c++",
    ".mm",
})
_ASSEMBLY_SUFFIXES = frozenset({".s", ".S", ".asm"})
_FLEX_SOURCE_SUFFIXES = frozenset({".l", ".ll", ".lpp"})
_BISON_SOURCE_SUFFIXES = frozenset({".y", ".yy", ".ypp"})
_TOOL_LABELS = {
    "ar": "AR",
    "as": "AS",
    "bison": "BISON",
    "cp": "COPY",
    "flex": "FLEX",
    "install": "INSTALL",
    "ld": "LD",
    "libtool": "LD",
    "ln": "SYMLINK",
    "make": "MAKE",
    "mkdir": "MKDIR",
    "nm": "NM",
    "objcopy": "OBJCOPY",
    "ranlib": "RANLIB",
    "strip": "STRIP",
}
_OPTIONS_WITH_VALUES = frozenset({
    "-C",
    "-D",
    "-f",
    "-g",
    "-j",
    "-m",
    "-o",
    "-T",
    "--directory",
    "--file",
    "--group",
    "--jobs",
    "--mode",
    "--owner",
    "--output",
    "--target-directory",
})


class BuildOutputParser:
    """Keep full build output while shortening the live status detail."""

    def __init__(
        self,
        *,
        work_dir: pathlib.PurePath | None = None,
        cwd: pathlib.PurePath | None = None,
        source_dirs: Sequence[pathlib.PurePath] = (),
    ) -> None:
        self._windows_paths = bool(work_dir and work_dir.drive)
        self._work_dir = self._path(work_dir) if work_dir is not None else None
        self._cwd = self._path(cwd) if cwd is not None else self._work_dir
        self._source_dirs = tuple(self._path(path) for path in source_dirs)
        self._make_dirs: dict[int, LexicalPath] = {}

    def __call__(self, entry: LogEntry) -> LogEntry:
        message = entry.get("message")
        if not isinstance(message, str) or not message:
            return entry
        entry["lograil.status.detail"] = self._short_invocation(message)
        return entry

    def _short_invocation(self, command: str) -> str:
        if command.lstrip().startswith("+"):
            return command

        make_directory = _MAKE_DIRECTORY_RE.fullmatch(command.strip())
        if make_directory is not None:
            action = make_directory.group("action").lower()
            level = int(make_directory.group("level") or 0)
            directory = self._path(make_directory.group("path"))
            path = self._display_directory(directory)
            if action == "entering":
                self._make_dirs[level] = directory
            else:
                self._make_dirs.pop(level, None)
            return f"MAKE {action} {path}"

        link = _LINK_RE.fullmatch(command.strip())
        if link is not None:
            source = self._display_path(_path_value(link.group("source")))
            target = self._display_path(_path_value(link.group("target")))
            return f"{link.group('label')} {source} -> {target}"

        try:
            tokens = shlex.split(command, posix=not self._windows_paths)
        except ValueError:
            return command

        compiler = self._compiler_invocation(tokens)
        if compiler is not None:
            return compiler

        for index, token in enumerate(tokens):
            tool = _canonical_tool(_portable_path(token).name)
            if tool is None:
                continue
            operand = self._tool_operand(tool, tokens[index + 1 :])
            if operand is not None:
                label = (
                    "AR"
                    if tool == "libtool" and "-static" in tokens
                    else _TOOL_LABELS[tool]
                )
                display = (
                    self._display_source_path(operand)
                    if tool in {"as", "bison", "flex"}
                    else self._display_path(operand)
                )
                return f"{label} {display}"
        return command

    def _compiler_invocation(self, tokens: Sequence[str]) -> str | None:
        compiler_index = next(
            (
                index
                for index, token in enumerate(tokens)
                if _COMPILER_RE.fullmatch(
                    _portable_path(token).name.removesuffix(".exe")
                )
            ),
            None,
        )
        if compiler_index is None:
            return None

        arguments = tokens[compiler_index + 1 :]
        for token in arguments:
            if token.startswith("-"):
                continue
            suffix = _portable_path(token).suffix
            if suffix in _C_SOURCE_SUFFIXES:
                return f"CC {self._display_source_path(token)}"
            if suffix in _CXX_SOURCE_SUFFIXES:
                return f"CXX {self._display_source_path(token)}"
            if suffix in _ASSEMBLY_SUFFIXES:
                return f"AS {self._display_source_path(token)}"

        output = _option_value(arguments, "-o", "--output")
        if output is not None:
            return f"LD {self._display_path(output)}"
        return None

    def _tool_operand(
        self,
        tool: str,
        arguments: Sequence[str],
    ) -> str | None:
        output = _option_value(arguments, "-o", "--output")
        if tool in {"ld", "libtool"} and output is not None:
            return output
        if tool == "as":
            source = _first_with_suffix(arguments, _ASSEMBLY_SUFFIXES)
            return source or output
        if tool == "ar":
            return _first_with_suffix(arguments, {".a", ".lib"})
        if tool == "flex":
            return _first_with_suffix(arguments, _FLEX_SOURCE_SUFFIXES)
        if tool == "bison":
            return _first_with_suffix(arguments, _BISON_SOURCE_SUFFIXES)
        if tool == "make":
            directory = _option_value(arguments, "-C", "--directory")
            if directory is not None:
                return directory
        operands = _operands(arguments)
        if not operands:
            return None
        return operands[-1]

    def _display_source_path(self, value: str) -> str:
        path = self._path(value)
        base = self._active_directory()
        resolved = (
            self._path(base / path)
            if not path.is_absolute() and base is not None
            else self._path(path)
        )
        containing = [
            directory
            for directory in (*self._make_dirs.values(), *self._source_dirs)
            if resolved.is_relative_to(directory)
        ]
        if containing:
            directory = max(
                containing,
                key=lambda candidate: len(candidate.parts),
            )
            return resolved.relative_to(directory).as_posix()
        return self._display_path(value)

    def _display_path(self, value: str) -> str:
        path = self._path(value)
        base = self._active_directory()
        if not path.is_absolute():
            if base is None:
                return value
            relative = (
                ntpath.relpath(self._path(base / path), base)
                if self._windows_paths
                else posixpath.relpath(self._path(base / path), base)
            )
            return self._path(relative).as_posix()
        resolved = self._path(path)
        containing = [
            directory
            for directory in self._candidate_directories()
            if resolved.is_relative_to(directory)
        ]
        if containing:
            directory = max(
                containing,
                key=lambda candidate: len(candidate.parts),
            )
            return resolved.relative_to(directory).as_posix()
        if self._work_dir is None:
            return value
        try:
            return resolved.relative_to(self._work_dir).as_posix()
        except ValueError:
            return value

    def _display_directory(self, directory: LexicalPath) -> str:
        if self._work_dir is not None and directory.is_relative_to(
            self._work_dir
        ):
            return directory.relative_to(self._work_dir).as_posix()
        return str(directory)

    def _active_directory(self) -> LexicalPath | None:
        if self._make_dirs:
            return self._make_dirs[max(self._make_dirs)]
        return self._cwd

    def _candidate_directories(self) -> tuple[LexicalPath, ...]:
        candidates = list(self._make_dirs.values())
        if self._cwd is not None:
            candidates.append(self._cwd)
        return tuple(candidates)

    def _path(self, path: str | pathlib.PurePath) -> LexicalPath:
        value = path.as_posix() if isinstance(path, pathlib.PurePath) else path
        if self._windows_paths:
            return pathlib.PureWindowsPath(ntpath.normpath(value))
        return pathlib.PurePosixPath(posixpath.normpath(value))


def _canonical_tool(name: str) -> str | None:
    normalized = name.removesuffix(".exe")
    for tool in _TOOL_LABELS:
        if normalized in {tool, f"g{tool}", f"llvm-{tool}"}:
            return tool
        if normalized.endswith(f"-{tool}"):
            return tool
    return None


def _portable_path(path: str) -> pathlib.PurePosixPath:
    return pathlib.PurePosixPath(path.replace("\\", "/"))


def _path_value(value: str) -> str:
    try:
        tokens = shlex.split(value)
    except ValueError:
        return value
    return tokens[0] if len(tokens) == 1 else value


def _option_value(arguments: Sequence[str], *options: str) -> str | None:
    for index, argument in enumerate(arguments):
        if argument in options and index + 1 < len(arguments):
            return arguments[index + 1]
        for option in options:
            if argument.startswith(f"{option}="):
                return argument.partition("=")[2]
            if (
                option == "-o"
                and argument.startswith("-o")
                and len(argument) > 2
            ):
                return argument[2:]
    return None


def _first_with_suffix(
    arguments: Sequence[str],
    suffixes: set[str] | frozenset[str],
) -> str | None:
    return next(
        (
            argument
            for argument in arguments
            if not argument.startswith("-")
            and _portable_path(argument).suffix in suffixes
        ),
        None,
    )


def _operands(arguments: Sequence[str]) -> list[str]:
    operands: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--":
            continue
        option = argument.partition("=")[0]
        if option in _OPTIONS_WITH_VALUES:
            skip_next = "=" not in argument
            continue
        if argument.startswith("-") or "=" in argument:
            continue
        operands.append(argument)
    return operands
