from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from martin_ai.platform import CURRENT_PLATFORM, system
from martin_ai.platform.macos.system import MacOSSystem


class PlatformTests(unittest.TestCase):
    def test_runtime_selects_current_backend(self):
        expected = {"windows": "windows", "darwin": "macos"}.get(CURRENT_PLATFORM, CURRENT_PLATFORM)
        self.assertEqual(system().name, expected)

    def test_macos_open_uses_native_open_command(self):
        with patch("martin_ai.platform.macos.system.subprocess.Popen") as popen:
            MacOSSystem().open_path(Path("/tmp/example"))
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "open")
        self.assertTrue(command[1].replace("\\", "/").endswith("/tmp/example"))

    def test_macos_placeholder_is_explicit(self):
        result = MacOSSystem().unsupported("Windows Event Log")
        self.assertFalse(result["success"])
        self.assertIn("macOS", result["error"])


if __name__ == "__main__":
    unittest.main()
