from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import tkinter as tk

SCREENSHOT_DIR = Path.home() / "Documents" / "MartinAI Screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def take_screenshot() -> dict[str, Any]:
    try:
        from PIL import ImageGrab
    except ImportError:
        return {"success": False, "error": "Run: pip install pillow"}

    try:
        image = ImageGrab.grab(all_screens=True)
        path = SCREENSHOT_DIR / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
        image.save(path)
        return {"success": True, "message": "Screenshot saved.", "path": str(path)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def read_clipboard() -> dict[str, Any]:
    root = tk.Tk()
    root.withdraw()
    try:
        text = root.clipboard_get()
        return {"success": True, "text": text[:20000], "truncated": len(text) > 20000}
    except tk.TclError:
        return {"success": True, "text": "", "message": "Clipboard has no text."}
    finally:
        root.destroy()


def write_clipboard(text: str) -> dict[str, Any]:
    root = tk.Tk()
    root.withdraw()
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        return {"success": True, "message": "Text copied to clipboard."}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        root.destroy()
