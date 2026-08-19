from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class MacOSSystem:
    """Safe initial macOS backend.

    Shared UI/core code can launch immediately. System-control capabilities stay
    unavailable until reviewed native implementations are added here.
    """

    name = "macos"

    def hidden_process_kwargs(self) -> dict[str, Any]:
        return {}

    def open_path(self, path: Path) -> None:
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def unsupported(self, feature: str) -> dict[str, Any]:
        return {"success": False, "error": f"{feature} has not been implemented for macOS yet."}
