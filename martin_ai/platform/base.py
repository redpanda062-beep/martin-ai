from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class SystemPlatform(Protocol):
    """Small interface used by shared code for operating-system services."""

    name: str

    def hidden_process_kwargs(self) -> dict[str, Any]: ...

    def open_path(self, path: Path) -> None: ...

    def unsupported(self, feature: str) -> dict[str, Any]: ...
