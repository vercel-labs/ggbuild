# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import json
import os
import pathlib
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest


class KeepworkIntegrationTest(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.cache = self.root / "cache"
        self.destination = self.root / "dist"
        self.trace = self.root / "trace"
        self.fail_once = self.root / "fail-once"
        self.environment = os.environ.copy()
        self.environment.update({
            "GGBUILD_KEEPWORK_FAIL_ONCE": str(self.fail_once),
            "GGBUILD_KEEPWORK_TRACE": str(self.trace),
            "LOGRAIL_OUTPUT": "plain",
            "XDG_CACHE_HOME": str(self.cache),
        })

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, *extra: str) -> list[str]:
        arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(
            platform.machine().lower(),
            platform.machine().lower(),
        )
        return [
            sys.executable,
            "-m",
            "ggbuild",
            "-vvv",
            "build",
            "--generic",
            "--keepwork",
            f"--arch={arch}",
            "--source-ref=1.0",
            f"--dest={self.destination}",
            *extra,
            "tests.keepwork_recipe:KeepworkRecipe",
        ]

    def run_build(
        self,
        *extra: str,
        success: bool,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            self.command(*extra),
            cwd=pathlib.Path(__file__).parent.parent,
            env=environment or self.environment,
            capture_output=True,
            text=True,
            check=False,
        )
        output = result.stdout + result.stderr
        if success and result.returncode:
            self.fail(f"build failed ({result.returncode}):\n{output}")
        if not success and not result.returncode:
            self.fail(f"build unexpectedly succeeded:\n{output}")
        return result

    def trace_lines(self) -> list[str]:
        return self.trace.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def build_stage_counts(lines: list[str]) -> tuple[int, ...]:
        return tuple(
            lines.count(stage) for stage in ("prepare", "configure", "build")
        )

    def workdir(self) -> pathlib.Path:
        candidates = list((self.cache / "ggbuild" / "work").iterdir())
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def assert_archive_contains_install(
        self,
        archive_path: pathlib.Path,
    ) -> None:
        with tarfile.open(archive_path) as archive:
            names = [member.name for member in archive.getmembers()]
        self.assertTrue(names)
        self.assertTrue(
            any("ggbuild-keepwork-fixture" in name for name in names)
        )

    def assert_direct_outputs(self) -> pathlib.Path:
        archives = list(self.destination.glob("*.tar.zst"))
        self.assertEqual(len(archives), 1)
        self.assert_archive_contains_install(archives[0])
        metadata = list(self.destination.glob("*.metadata.json"))
        self.assertEqual(len(metadata), 1)
        fqname = metadata[0].name.removesuffix(".metadata.json")
        self.assertEqual(
            archives[0].name,
            f"{fqname}.tar.zst",
        )
        self.assertFalse((self.destination / "build-metadata.json").exists())
        return archives[0]

    def test_keepwork_resume_validation_recovery_and_clean(self) -> None:
        self.fail_once.touch()
        self.run_build("--clean", success=False)
        self.assertEqual(
            self.trace_lines(),
            ["prepare", "configure", "build", "build_install"],
        )

        workdir = self.workdir()
        stages = workdir / ".ggbuild-checkpoints" / "stages"
        package_stages = next((stages / "packages").iterdir())
        self.assertTrue((package_stages / "prepare").is_file())
        self.assertTrue((package_stages / "configure").is_file())
        self.assertTrue((package_stages / "build").is_file())
        self.assertFalse((package_stages / "build_install").exists())

        self.run_build(success=True)
        self.assertEqual(
            self.trace_lines(),
            [
                "prepare",
                "configure",
                "build",
                "build_install",
                "build_install",
            ],
        )
        self.assertTrue((package_stages / "build_install").is_file())
        self.assertTrue((package_stages / "install").is_file())

        archive = self.assert_direct_outputs()
        deployment = self.root / "deployment"
        with tarfile.open(archive) as archive_file:
            archive_file.extractall(deployment, filter="data")
        executables = [
            path
            for path in deployment.glob(
                f"**/hello{'.exe' if os.name == 'nt' else ''}"
            )
            if path.is_file()
        ]
        self.assertTrue(executables)
        execution = subprocess.run(
            [str(executables[0])],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(execution.stdout, "hello from keepwork\n")

        archive.unlink()
        self.run_build(success=True)
        self.assertTrue(archive.is_file())

        manifest_path = workdir / ".ggbuild-checkpoints" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["compression"] = ["incompatible"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before_repackage = self.trace_lines()
        self.run_build(success=True)
        self.assertEqual(
            self.build_stage_counts(self.trace_lines()),
            self.build_stage_counts(before_repackage),
        )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["packages"][0]["scripts"]["build"] = "incompatible"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.run_build(success=True)
        self.assertEqual(
            self.trace_lines()[-2:],
            ["build", "build_install"],
        )

        before_reuse = self.trace_lines()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["packages"][0]["scripts"]["build"] = "stale-for-reuse"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.run_build(
            "--node-cache=tests.keepwork_recipe:KeepworkRecipe=reuse",
            success=True,
        )
        self.assertEqual(
            self.build_stage_counts(self.trace_lines()),
            self.build_stage_counts(before_reuse),
        )
        retained = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            retained["stale_reused_packages"],
            [retained["packages"][0]["unique_name"]],
        )

        self.run_build(success=True)
        self.assertEqual(
            self.trace_lines()[-2:],
            ["build", "build_install"],
        )

        self.trace.unlink()
        self.run_build("--clean", success=True)
        self.assertEqual(
            self.trace_lines(),
            ["prepare", "configure", "build", "build_install"],
        )
