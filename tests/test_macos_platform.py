# SPDX-PackageName: ggbuild
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright Vercel, Inc. and the contributors.

from __future__ import annotations

import unittest
from unittest import mock

from ggbuild.targets.macos.platform import MacOSPortableTarget


class MacOSPlatformTest(unittest.TestCase):
    def test_prepare_serializes_homebrew_installation(self) -> None:
        target = MacOSPortableTarget("aarch64")

        with (
            mock.patch(
                "ggbuild.targets.macos.platform.shutil.which",
                return_value="/opt/homebrew/bin/brew",
            ),
            mock.patch("ggbuild.targets.macos.platform.tools.cmd") as command,
        ):
            target.prepare()

        command.assert_called_once()
        args = command.call_args.args
        self.assertEqual(args[0], "lockf")
        self.assertTrue(
            any("HOMEBREW_NO_AUTO_UPDATE=1" in str(arg) for arg in args)
        )
        self.assertNotIn("update", args)
        self.assertNotIn("upgrade", args)
        self.assertIn("sccache", args)


if __name__ == "__main__":
    unittest.main()
