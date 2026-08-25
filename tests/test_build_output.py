# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import pathlib
import unittest

from ggbuild.targets.generic.output import BuildOutputParser


class BuildOutputParserTest(unittest.TestCase):
    def test_shortens_common_build_tools(self) -> None:
        work_dir = pathlib.Path("/work/build-id")
        parser = BuildOutputParser(work_dir=work_dir)
        cases = [
            (
                (
                    "clang -I/opt/include -O2 -c "
                    "/work/build-id/pkg/main.c -o main.o"
                ),
                "CC pkg/main.c",
            ),
            (
                "aarch64-linux-gnu-gcc -DNAME=value ../../port.c -c",
                "CC ../../port.c",
            ),
            (
                "ccache /usr/bin/clang++ -std=c++20 -c 'source/a file.cpp'",
                "CXX source/a file.cpp",
            ),
            (
                "/bin/sh ./libtool --mode=compile g++ -c source/widget.cc",
                "CXX source/widget.cc",
            ),
            (
                "cc first.o second.o -o /work/build-id/pkg/server",
                "LD pkg/server",
            ),
            (
                "aarch64-linux-gnu-ld -o pkg/program pkg/main.o",
                "LD pkg/program",
            ),
            (
                "gar rcs /work/build-id/pkg/libthing.a thing.o",
                "AR pkg/libthing.a",
            ),
            ("glibtool -static -o pkg/libthing.a thing.o", "AR pkg/libthing.a"),
            ("gas -o thing.o /work/build-id/pkg/thing.S", "AS pkg/thing.S"),
            ("granlib /work/build-id/pkg/libthing.a", "RANLIB pkg/libthing.a"),
            (
                "ginstall -m 755 program /work/build-id/image/bin/program",
                "INSTALL image/bin/program",
            ),
            (
                "gstrip -S /work/build-id/image/bin/program",
                "STRIP image/bin/program",
            ),
            (
                "gobjcopy input /work/build-id/image/output",
                "OBJCOPY image/output",
            ),
            ("gmake -C /work/build-id/pkg all", "MAKE pkg"),
            (
                (
                    "gmake[2]: Entering directory "
                    "'/work/build-id/postgresql/_artifacts/build/openssl'"
                ),
                "MAKE entering postgresql/_artifacts/build/openssl",
            ),
            (
                (
                    "make[1]: Leaving directory "
                    "\u2018/work/build-id/postgresql/_artifacts/build/icu"
                    "\u2019"
                ),
                "MAKE leaving postgresql/_artifacts/build/icu",
            ),
            ("gcp source /work/build-id/image/file", "COPY image/file"),
            ("gln -s source /work/build-id/image/link", "SYMLINK image/link"),
            ("gmkdir -p /work/build-id/image/lib", "MKDIR image/lib"),
            ("gnm /work/build-id/pkg/program", "NM pkg/program"),
            (
                "flex -o scan.c /work/build-id/pkg/src/scan.l",
                "FLEX pkg/src/scan.l",
            ),
            (
                (
                    "bison --defines=parse.h -o parse.c "
                    "/work/build-id/pkg/src/parse.y"
                ),
                "BISON pkg/src/parse.y",
            ),
            (
                "gflex --outfile=scan.cc /work/build-id/pkg/src/scan.ll",
                "FLEX pkg/src/scan.ll",
            ),
            (
                ("aarch64-linux-gnu-bison -d /work/build-id/pkg/src/parse.yy"),
                "BISON pkg/src/parse.yy",
            ),
            (
                "checking whether the C compiler works... yes",
                "checking whether the C compiler works... yes",
            ),
            (
                "  + cc -c /work/build-id/pkg/traced.c -o traced.o",
                "  + cc -c /work/build-id/pkg/traced.c -o traced.o",
            ),
            ("CC already-terse.o", "CC already-terse.o"),
        ]

        for command, detail in cases:
            with self.subTest(command=command):
                entry = {"message": command}

                result = parser(entry)

                self.assertEqual(result["message"], command)
                self.assertEqual(result["lograil.status.detail"], detail)

    def test_preserves_absolute_paths_outside_work_directory(self) -> None:
        parser = BuildOutputParser(work_dir=pathlib.Path("/work/build-id"))
        command = "cc -c /external/source.c"

        result = parser({"message": command})

        self.assertEqual(
            result["lograil.status.detail"],
            "CC /external/source.c",
        )

    def test_paths_are_relative_to_deepest_active_make_directory(self) -> None:
        parser = BuildOutputParser(
            work_dir=pathlib.PurePosixPath("/work/build-id"),
            cwd=pathlib.PurePosixPath("/work/build-id/postgresql"),
        )
        source_dir = pathlib.PurePosixPath(
            "/work/build-id/postgresql/postgresql/postgresql"
        )
        parser({
            "message": f"gmake[1]: Entering directory '{source_dir}'",
        })

        result = parser({
            "message": (
                "cc -c "
                f"{source_dir}/src/backend/utils/adt/jsonfuncs.c "
                "-o jsonfuncs.o"
            ),
        })

        self.assertEqual(
            result["lograil.status.detail"],
            "CC src/backend/utils/adt/jsonfuncs.c",
        )

    def test_relative_paths_are_normalized_against_active_make_directory(
        self,
    ) -> None:
        parser = BuildOutputParser(
            work_dir=pathlib.Path("/work/build-id"),
            cwd=pathlib.Path("/work/build-id/pkg/build/subdir"),
        )

        result = parser({"message": "cc -c ../src/../src/main.c"})

        self.assertEqual(result["lograil.status.detail"], "CC ../src/main.c")

    def test_compiler_source_is_relative_to_thirdparty_source_dir(self) -> None:
        work_dir = pathlib.PurePosixPath("/work/build-id")
        source_root = work_dir / "postgresql"
        parser = BuildOutputParser(
            work_dir=work_dir,
            cwd=source_root,
            source_dirs=(source_root / "thirdparty/icu",),
        )
        build_dir = source_root / "_artifacts/build/icu"
        parser({
            "message": f"gmake[1]: Entering directory '{build_dir}'",
        })

        result = parser({
            "message": (
                "c++ -c ../../../../postgresql/thirdparty/icu/"
                "source/i18n/nultrans.cpp"
            ),
        })

        self.assertEqual(
            result["lograil.status.detail"],
            "CXX source/i18n/nultrans.cpp",
        )

    def test_normalizes_link_status_paths(self) -> None:
        work_dir = pathlib.PurePosixPath("/work/build-id")
        parser = BuildOutputParser(
            work_dir=work_dir,
            cwd=work_dir / "postgresql",
        )
        build_dir = work_dir / "postgresql/_artifacts/build/openssl"
        install_dir = (
            build_dir
            / "../../../postgresql/../_artifacts/install/openssl"
            / "opt/postgresql/lib"
        )

        result = parser({
            "message": (
                f"link {install_dir}/libcrypto.dylib -> "
                f"{install_dir}/libcrypto.4.dylib"
            ),
        })

        self.assertEqual(
            result["lograil.status.detail"],
            "link _artifacts/install/openssl/opt/postgresql/lib/"
            "libcrypto.dylib -> "
            "_artifacts/install/openssl/opt/postgresql/lib/"
            "libcrypto.4.dylib",
        )

    def test_normalizes_native_windows_paths(self) -> None:
        parser = BuildOutputParser(
            work_dir=pathlib.PureWindowsPath(r"C:\work\build-id"),
        )

        result = parser({
            "message": r"gcc.exe -c C:\work\build-id\pkg\main.c",
        })

        self.assertEqual(result["lograil.status.detail"], "CC pkg/main.c")

    def test_leaves_non_string_message_alone(self) -> None:
        entry = {"message": 42}

        result = BuildOutputParser()(entry)

        self.assertEqual(result, entry)


if __name__ == "__main__":
    unittest.main()
