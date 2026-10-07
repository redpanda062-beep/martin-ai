from __future__ import annotations

import base64
import os
import subprocess
import sys
from typing import Any, Mapping

from .windows_process import hidden_process_kwargs


def run_powershell(script: str, *, timeout: int = 60,
                   parameters: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Execute application-owned PowerShell without a shell or visible console.

    Dynamic values are passed through uniquely named environment variables, never
    interpolated into the script. Callers must supply application-owned scripts.
    """
    if sys.platform != "win32":
        return {"success": False, "started": False, "exit_code": None, "stdout": "", "stderr": "",
                "timed_out": False, "unavailable": True, "message": "Windows PowerShell is only available on Windows."}
    environment = os.environ.copy()
    for name, value in (parameters or {}).items():
        if not name.isidentifier() or not name.isupper():
            raise ValueError("PowerShell parameter names must be uppercase identifiers.")
        environment[f"MARTIN_AI_PS_{name}"] = str(value)
    unicode_prefix = "$ProgressPreference='SilentlyContinue';$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
    encoded = base64.b64encode((unicode_prefix + script).encode("utf-16-le")).decode("ascii")
    arguments = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "RemoteSigned", "-EncodedCommand", encoded]
    try:
        completed = subprocess.run(arguments, shell=False, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=max(1, int(timeout)),
                                   env=environment, **hidden_process_kwargs())
        stdout, stderr = completed.stdout.strip(), completed.stderr.strip()
        return {"success": completed.returncode == 0, "started": True, "exit_code": completed.returncode,
                "stdout": stdout, "stderr": stderr, "timed_out": False, "unavailable": False,
                "message": stdout or stderr}
    except subprocess.TimeoutExpired as exc:
        return {"success": False, "started": True, "exit_code": None,
                "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
                "timed_out": True, "unavailable": False, "message": "PowerShell operation timed out."}
    except OSError as exc:
        return {"success": False, "started": False, "exit_code": None, "stdout": "", "stderr": str(exc),
                "timed_out": False, "unavailable": True, "message": str(exc)}
