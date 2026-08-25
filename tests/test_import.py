# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

import subprocess
import sys
import unittest

import ggbuild


class ImportTest(unittest.TestCase):
    def test_version_is_available(self) -> None:
        self.assertTrue(ggbuild.__version__)

    def test_packages_import_in_clean_interpreter(self) -> None:
        subprocess.run(
            [sys.executable, "-c", "import ggbuild.packages"],
            check=True,
        )
