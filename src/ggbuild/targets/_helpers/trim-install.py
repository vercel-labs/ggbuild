#!/usr/bin/env python3
# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.


import argparse
import pathlib
import shutil
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("install_list", help="List of files to be installed.")
    parser.add_argument(
        "no_install_list",
        help="List of files that should not be installed "
        "even if in install list.",
    )
    parser.add_argument(
        "ignore_list",
        help="List of files that are intentionally not installed.",
    )
    parser.add_argument("install_dir", help="Installation directory.")
    parser.add_argument(
        "--keep-files",
        action="store_true",
        help="Generate the package file list without pruning the install tree.",
    )

    args = parser.parse_args()

    install_dir = pathlib.Path(args.install_dir)

    with pathlib.Path(args.install_list).open("r", encoding="utf-8") as f:
        install_set = {line.strip() for line in f}

    with pathlib.Path(args.no_install_list).open("r", encoding="utf-8") as f:
        no_install_set = {line.strip() for line in f}

    with pathlib.Path(args.ignore_list).open("r", encoding="utf-8") as f:
        ignore_set = {line.strip() for line in f}

    to_remove = (ignore_set - install_set) | no_install_set

    if not args.keep_files:
        for path in sorted(to_remove, reverse=True):
            full_path = install_dir / path
            print(f"Removing {path}", file=sys.stderr)
            if full_path.is_dir() and not full_path.is_symlink():
                shutil.rmtree(str(full_path))
            elif full_path.exists() or full_path.is_symlink():
                pathlib.Path(str(full_path)).unlink()

    for path in install_set - no_install_set:
        full_path = install_dir / path
        if not full_path.is_dir():
            print(path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
