from __future__ import annotations

from pathlib import Path
from typing import Any


class UnsupportedSystem:
    def __init__(self, name: str) -> None:
        self.name = name

    def hidden_process_kwargs(self) -> dict[str, Any]:
        return {}

    def open_path(self, path: Path) -> None:
        raise OSError(f"Opening folders is not implemented for {self.name}.")

    def unsupported(self, feature: str) -> dict[str, Any]:
        return {"success": False, "error": f"{feature} is unavailable on {self.name}."}
