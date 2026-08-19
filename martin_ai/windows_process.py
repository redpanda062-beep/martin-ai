from __future__ import annotations

from typing import Any

from .platform import system


def hidden_process_kwargs() -> dict[str, Any]:
    """Prevent console programs from flashing a window when started by the GUI."""
    return system().hidden_process_kwargs()
