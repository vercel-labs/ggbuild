from typing import Any

import argparse
import csv
import pathlib
import sys

import toml


def write_line(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def main() -> None:
    csv.field_size_limit(sys.maxsize)

    parser = argparse.ArgumentParser()
    parser.add_argument("cargo_lock_file", type=str, help="path to Cargo.lock")
    parser.add_argument(
        "cargo_io_dump",
        type=str,
        help="path to decompressed crates.io db-dump "
        "(https://crates.io/data-access)",
    )

    args = parser.parse_args()

    data = toml.load(args.cargo_lock_file)
    db_path = pathlib.Path(args.cargo_io_dump) / "data"

    crates: dict[str, dict[str, Any]] = {}
    crates_id_to_name: dict[str, str] = {}

    with (db_path / "crates.csv").open(encoding="utf-8", newline="") as csvfile:
        reader = csv.reader(csvfile, delimiter=",")
        header = next(reader)
        for row in reader:
            crate_info = dict(zip(header, row, strict=True))
            crates[crate_info["name"]] = crate_info
            crates_id_to_name[crate_info["id"]] = crate_info["name"]

    with (db_path / "versions.csv").open(
        encoding="utf-8", newline=""
    ) as csvfile:
        reader = csv.reader(csvfile, delimiter=",")
        header = next(reader)
        for row in reader:
            crate_info = dict(zip(header, row, strict=True))
            name = crates_id_to_name[crate_info["crate_id"]]
            lic = crate_info["license"]
            crates[name].setdefault("license", {})[crate_info["num"]] = lic

    failed_for: dict[str, str] = {}
    deps: dict[str, str] = {}
    for package in data["package"]:
        name, ver = package["name"], package["version"]

        if name not in crates:
            failed_for[name] = "not on crates.io"
        else:
            crate_data = crates[name]
            crate_license = crate_data["license"]
            try:
                deps[name] = crate_license[ver]
            except KeyError:
                failed_for[name] = f"could not find license for version {ver}"

    write_line("Dependencies:\n")
    for name, license_name in deps.items():
        write_line(f"{name}: {license_name}")
    if failed_for:
        write_line("\n\nFailed to resolve")
        for name, reason in failed_for.items():
            write_line(f"{name}: {reason}")


if __name__ == "__main__":
    main()
