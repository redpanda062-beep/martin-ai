from __future__ import annotations

import platform as stdlib_platform
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import SystemPlatform


CURRENT_PLATFORM = stdlib_platform.system().lower()


def is_windows() -> bool:
    return CURRENT_PLATFORM == "windows"


def is_macos() -> bool:
    return CURRENT_PLATFORM == "darwin"


@lru_cache(maxsize=1)
def system() -> "SystemPlatform":
    """Load only the backend for the operating system currently running."""
    if is_windows():
        from .windows.system import WindowsSystem

        return WindowsSystem()
    if is_macos():
        from .macos.system import MacOSSystem

        return MacOSSystem()
    from .unsupported import UnsupportedSystem

    return UnsupportedSystem(CURRENT_PLATFORM or "unknown")
