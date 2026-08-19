"""Windows-only Martin AI services. Never import this package on macOS."""

from .system import WindowsSystem

__all__ = ["WindowsSystem"]
