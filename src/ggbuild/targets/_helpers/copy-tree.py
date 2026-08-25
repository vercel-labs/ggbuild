#!/usr/bin/env python3
# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

from typing import (
    NoReturn,
)

import argparse
import logging
import os
import pathlib
import platform
import shutil
import stat
import sys
from collections.abc import Collection, Iterable, Iterator

logger = logging.getLogger("copy-tree")
system = platform.system()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copies a tree of files to an empty directory."
    )
    parser.add_argument(
        "src",
        help=(
            "Source directory. To only add the contents of this directory,"
            " append / at the end."
        ),
    )
    parser.add_argument(
        "dest", help="Destination directory. Created if doesn't exist."
    )
    parser.add_argument(
        "--files-from",
        help="Optional list of files to copy from the source directory.",
    )
    parser.add_argument(
        "--flatten",
        help="Copy all files to the top directory.",
        action="store_true",
    )
    parser.add_argument(
        "--dereference",
        help="Copy the contents of symbolic links instead of the links.",
        action="store_true",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Show information on each file copied, directory made, etc.",
        action="store_true",
    )
    return parser.parse_args()


def die(msg: str) -> NoReturn:
    logger.error(msg)
    sys.exit(1)


def main(
    src: str,
    dest: str,
    *,
    files_from: str | None,
    flatten: bool,
    dereference: bool,
) -> None:
    dest = ensure_destination(src, dest)
    all_files = list(ensure_relative(get_paths_in(src), src))

    if files_from:
        p = pathlib.Path(files_from)
        relative_files = list(
            ensure_relative(p.read_text(encoding="utf-8").splitlines(), src)
        )
        relative_files = add_missing_directory_entries(relative_files)
        logger.info(
            "Using file list in %s with %d entries", p, len(relative_files)
        )
        warn_about_excluded_files(included=relative_files, all_files=all_files)
        copy_files(
            src,
            dest,
            relative_files,
            flatten=flatten,
            dereference=dereference,
        )
    else:
        logger.info(
            "No file list given; copying all %d entries", len(all_files)
        )
        copy_files(
            src,
            dest,
            all_files,
            flatten=flatten,
            dereference=dereference,
        )


def ensure_destination(src: str, dest: str) -> str:
    src_p = pathlib.Path(src)
    dest_p = pathlib.Path(dest)
    if not src.endswith(("/", "\\")):
        # To mimic rsync behavior
        dest_p /= src_p.name
    if dest_p.exists():
        if not dest_p.is_dir():
            raise ValueError(f"{dest} is not a directory, cannot continue")
        if any(dest_p.iterdir()):
            logger.warning("Directory %s is not empty", dest)
    else:
        pathlib.Path(dest_p).mkdir(
            parents=True
        )  # no error handling, irrecoverable
    return str(dest_p)


def get_paths_in(directory: str) -> Iterator[str]:
    for root, dirs, files in os.walk(directory):
        root_p = pathlib.Path(root).relative_to(directory)
        for name in dirs:
            yield str(root_p / name)
        for name in files:
            yield str(root_p / name)


def ensure_relative(files: Iterable[str], root: str) -> Iterator[str]:
    root_p = pathlib.Path(root).resolve()
    for path in files:
        p = pathlib.Path(path)
        if p.is_absolute():
            p_treated_as_relative = root_p / str(p)[1:]
            if p_treated_as_relative.exists():
                p = p_treated_as_relative
            yield str(p.relative_to(root_p))
            continue

        abs_p = root_p / p
        if abs_p.is_symlink() or abs_p.exists():
            # a symlink might point to a non-existent file
            yield path
            continue

        if p.parts[0] == root_p.name:
            # file list element looks "off-by-one", created
            # outside of the directory given as `src` to the tool
            lose_one_level = p.relative_to(root_p.name)
            if (root_p / lose_one_level).exists():
                yield str(lose_one_level)
                continue

        logger.error("File in file list doesn't exist: %s", path)


def copy_files(
    src: str,
    dest: str,
    files: Iterable[str],
    *,
    flatten: bool,
    dereference: bool,
) -> None:
    """Copy files listed in `files` from `src` to `dest`.

    Paths in `files` must be relative.
    """
    src_dir = pathlib.Path(src)
    dest_dir = pathlib.Path(dest)
    basenames: dict[str, str] = {}
    for src_file in files:
        path_from = src_dir / src_file
        dest_file = _destination_name(
            path_from, src_file, flatten=flatten, basenames=basenames
        )
        if dest_file is None:
            continue
        path_to = dest_dir / dest_file
        _copy_path(path_from, path_to, dereference=dereference)
        _copy_metadata(path_from, path_to)


def _destination_name(
    path_from: pathlib.Path,
    src_file: str,
    *,
    flatten: bool,
    basenames: dict[str, str],
) -> str | None:
    if not flatten:
        return src_file
    if path_from.is_dir() or path_from.is_symlink():
        kind = "directory" if path_from.is_dir() else "symlink"
        logger.info("Skipping %s %s due to --flatten", kind, src_file)
        return None
    basename = path_from.name
    if previous := basenames.get(basename):
        die(
            "`copy-tree --flatten` encountered a duplicate file name:"
            f" {src_file} and {previous}"
        )
    basenames[basename] = src_file
    return basename


def _copy_path(
    path_from: pathlib.Path,
    path_to: pathlib.Path,
    *,
    dereference: bool,
) -> None:
    if path_from.is_dir():
        if path_to.is_dir():
            logger.warning("Directory %s already exists", path_to)
            return
        try:
            path_to.mkdir(parents=True)
        except OSError:
            logger.exception("Failed making the %s directory", path_to)
        else:
            logger.info("mkdir %s", path_to)
        return
    if path_to.exists():
        logger.warning(
            "File %s already exists and will be overwritten", path_to
        )
    try:
        shutil.copyfile(
            path_from,
            path_to,
            follow_symlinks=dereference,
        )
    except OSError:
        logger.exception("Failed copying %s -> %s", path_from, path_to)
    else:
        logger.info("cp %s -> %s", path_from, path_to)


def _copy_metadata(path_from: pathlib.Path, path_to: pathlib.Path) -> None:
    stat_from = path_from.lstat()
    stat_to = path_to.lstat()
    executable_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    new_mode = stat_to.st_mode | (stat_from.st_mode & executable_bits)
    if new_mode != stat_to.st_mode:
        try:
            path_to.chmod(new_mode)
        except OSError:
            logger.exception("Failed chmodding %s to %#o", path_to, new_mode)
        else:
            logger.info("chmod %#o %s", new_mode, path_to)
    if system == "Windows":
        return
    try:
        os.utime(
            path_to,
            (stat_from.st_atime, stat_from.st_mtime),
            follow_symlinks=False,
        )
    except OSError, NotImplementedError:
        logger.exception("Failed setting times on %s", path_to)


def warn_about_excluded_files(
    included: Collection[str], all_files: Collection[str]
) -> None:
    last_seen = ""

    def maybe_warn() -> None:
        if last_seen:
            logger.warning("Not in file list: %s", last_seen)

    for excluded_file in sorted(set(all_files) - set(included)):
        skip = last_seen.endswith("/") and excluded_file.startswith(last_seen)
        if not skip:
            maybe_warn()
        last_seen = excluded_file
    maybe_warn()


def add_missing_directory_entries(files: Iterable[str]) -> list[str]:
    dirs: set[pathlib.Path] = {pathlib.Path()}
    result: set[str] = set()
    for file_entry in files:
        normalized_file = file_entry.removesuffix("/")
        for parent in reversed(pathlib.Path(normalized_file).parents):
            if parent not in dirs:
                dirs.add(parent)
                result.add(str(parent))
        result.add(normalized_file)
    return sorted(result)


if __name__ == "__main__":
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s - %(levelname)s: %(message)s",
    )
    main(
        args.src,
        args.dest,
        files_from=args.files_from,
        flatten=args.flatten,
        dereference=args.dereference,
    )
