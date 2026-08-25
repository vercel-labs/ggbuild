# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import gzip
import pathlib
import tempfile
import unittest
from compression import zstd
from unittest import mock

from ggbuild.targets.generic.build import compress_file


class CompressionTest(unittest.TestCase):
    def test_compresses_with_progress(self) -> None:
        content = b"ggbuild compression\n" * 100_000

        for encoding, suffix, opener in (
            ("zstd", ".zst", zstd.open),
            ("gzip", ".gz", gzip.open),
        ):
            with (
                self.subTest(encoding=encoding),
                tempfile.TemporaryDirectory() as td,
            ):
                root = pathlib.Path(td)
                source = root / "artifact.tar"
                destination = root / f"artifact.tar{suffix}"
                source.write_bytes(content)
                handle = mock.MagicMock()
                context = mock.MagicMock()
                context.__enter__.return_value = handle

                with mock.patch(
                    "ggbuild.targets.generic.build.progress",
                    return_value=context,
                ) as progress_mock:
                    compress_file(
                        source,
                        destination,
                        encoding=encoding,
                    )

                with opener(destination, "rb") as compressed:
                    self.assertEqual(compressed.read(), content)
                progress_mock.assert_called_once_with(
                    process="compressing",
                    subject=destination.name,
                    description="reading artifact.tar",
                    total=len(content),
                )
                self.assertEqual(
                    sum(call.args[0] for call in handle.advance.call_args_list),
                    len(content),
                )

    def test_removes_partial_output_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            source = root / "artifact.tar"
            destination = root / "artifact.tar.zst"
            source.write_bytes(b"content")

            with self.assertRaises(ValueError):
                compress_file(source, destination, encoding="unknown")

            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
