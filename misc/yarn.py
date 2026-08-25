from typing import Any, cast

import argparse
import asyncio
import pathlib
import sys

import httpx
import yaml

CONCURRENCY = 20


def write_line(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("yarn_lock_file", type=str, help="path to Cargo.lock")

    args = parser.parse_args()

    with pathlib.Path(args.yarn_lock_file).open(encoding="utf-8") as stream:
        lock_data = cast("dict[str, dict[str, Any]]", yaml.safe_load(stream))

    deps: dict[str, str] = {}
    for package_spec, package_data in lock_data.items():
        if package_spec == "__metadata":
            continue
        if package_spec.startswith("root-workspace"):
            continue
        first_spec = package_spec.split(", ", maxsplit=1)[0]
        package_name, _, _ = first_spec.rpartition("@")
        if "@patch:" in package_name:
            package_name, _, _ = package_name.rpartition("@patch:")
        if package_name.startswith("@edgedb/"):
            # our own packages
            continue
        deps[package_name] = package_data["version"]

    sem = asyncio.Semaphore(CONCURRENCY)

    failed_for: dict[str, str] = {}

    async def worker() -> None:
        async with httpx.AsyncClient() as client:
            while True:
                async with sem:
                    try:
                        package, _version = deps.popitem()
                    except KeyError:
                        return

                    url = f"https://registry.npmjs.com/{package}"
                    resp = await client.get(url)

                    package_metadata = resp.json()

                    try:
                        write_line(f"{package}: {package_metadata['license']}")
                    except KeyError:
                        failed_for[package] = "no license data on npmjs.com"

    await asyncio.gather(*[worker() for _ in range(CONCURRENCY)])

    if failed_for:
        write_line("\n\nFailed for:")
        for name, reason in failed_for.items():
            write_line(f"{name}: {reason}")


if __name__ == "__main__":
    asyncio.run(main())
