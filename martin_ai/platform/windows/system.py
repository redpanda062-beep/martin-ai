from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any


class WindowsSystem:
    name = "windows"

    def hidden_process_kwargs(self) -> dict[str, Any]:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}

    def open_path(self, path: Path) -> None:
        os.startfile(path)

    def unsupported(self, feature: str) -> dict[str, Any]:
        return {"success": False, "error": f"{feature} is unavailable on Windows."}
