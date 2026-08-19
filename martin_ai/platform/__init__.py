"""Runtime-selected operating-system services.

Shared code imports this package, never a concrete backend. Platform modules are
loaded lazily so Windows APIs cannot be imported on macOS and vice versa.
"""

from .runtime import CURRENT_PLATFORM, is_macos, is_windows, system

__all__ = ["CURRENT_PLATFORM", "is_macos", "is_windows", "system"]
