from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from martin_ai.powershell import run_powershell


class PowerShellRunnerTests(unittest.TestCase):
    @patch("martin_ai.powershell.hidden_process_kwargs", return_value={})
    @patch("martin_ai.powershell.subprocess.run")
    def test_uses_encoded_command_without_shell(self, process, _hidden) -> None:
        process.return_value = subprocess.CompletedProcess([], 0, "héllo", "")
        with patch("martin_ai.powershell.sys.platform", "win32"):
            result = run_powershell("Write-Output $env:MARTIN_AI_PS_VALUE", parameters={"VALUE": "a ' quoted value"})
        self.assertTrue(result["success"]); self.assertEqual(result["stdout"], "héllo")
        arguments = process.call_args.args[0]
        self.assertIn("-EncodedCommand", arguments); self.assertNotIn("a ' quoted value", " ".join(arguments))
        self.assertFalse(process.call_args.kwargs["shell"])
        self.assertEqual(process.call_args.kwargs["env"]["MARTIN_AI_PS_VALUE"], "a ' quoted value")

    def test_rejects_invalid_parameter_name(self) -> None:
        with patch("martin_ai.powershell.sys.platform", "win32"):
            with self.assertRaises(ValueError): run_powershell("Write-Output 1", parameters={"bad-name": "x"})

    def test_is_windows_only(self) -> None:
        with patch("martin_ai.powershell.sys.platform", "linux"):
            result = run_powershell("Write-Output 1")
        self.assertFalse(result["success"]); self.assertTrue(result["unavailable"])


if __name__ == "__main__":
    unittest.main()
