"""macOS-only Martin AI services. Never import this package on Windows."""

from .system import MacOSSystem

__all__ = ["MacOSSystem"]
