from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..platform import system

ALLOWED_TEXT_EXTENSIONS = {
    ".txt", ".log", ".ini", ".cfg", ".json", ".xml",
    ".yaml", ".yml", ".md", ".csv",
}


def _default_search_roots() -> list[Path]:
    home = Path.home()
    roots = [home / "Desktop", home / "Downloads", home / "Documents", home / "Pictures"]
    return [root for root in roots if root.exists()]


def search_files(query: str, root: str = "home", limit: int = 50) -> dict[str, Any]:
    query = query.strip().lower()
    if not query:
        return {"success": False, "error": "Search query cannot be empty."}

    limit = max(1, min(limit, 200))

    if root.strip().lower() == "home":
        roots = _default_search_roots()
    else:
        requested = Path(os.path.expandvars(root)).expanduser().resolve()
        if not requested.exists() or not requested.is_dir():
            return {"success": False, "error": f"Folder does not exist: {requested}"}
        roots = [requested]

    matches = []
    skipped = 0

    for search_root in roots:
        try:
            for path in search_root.rglob("*"):
                if len(matches) >= limit:
                    break
                try:
                    if query not in path.name.lower():
                        continue
                    stat = path.stat()
                    matches.append(
                        {
                            "name": path.name,
                            "path": str(path),
                            "type": "folder" if path.is_dir() else "file",
                            "size_mb": round(stat.st_size / 2**20, 2) if path.is_file() else None,
                        }
                    )
                except (PermissionError, OSError):
                    skipped += 1
        except (PermissionError, OSError):
            skipped += 1

        if len(matches) >= limit:
            break

    return {
        "success": True,
        "query": query,
        "matches": matches,
        "count": len(matches),
        "skipped": skipped,
        "limited": len(matches) >= limit,
    }


def read_text_file(path: str, max_chars: int = 16000) -> dict[str, Any]:
    file_path = Path(os.path.expandvars(path)).expanduser().resolve()

    if not file_path.exists() or not file_path.is_file():
        return {"success": False, "error": f"File does not exist: {file_path}"}

    if file_path.suffix.lower() not in ALLOWED_TEXT_EXTENSIONS:
        return {
            "success": False,
            "error": f"Unsupported file type: {file_path.suffix or '(no extension)'}",
        }

    max_chars = max(1000, min(max_chars, 50000))
    text = file_path.read_text(encoding="utf-8", errors="replace")

    return {
        "success": True,
        "path": str(file_path),
        "content": text[:max_chars],
        "truncated": len(text) > max_chars,
        "characters": len(text),
    }


def open_user_folder(folder_name: str) -> dict[str, Any]:
    from .diagnostics import REPORT_DIR

    folders = {
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "documents": Path.home() / "Documents",
        "pictures": Path.home() / "Pictures",
        "videos": Path.home() / "Videos",
        "music": Path.home() / "Music",
        "reports": REPORT_DIR,
    }

    normalized = folder_name.strip().lower()
    folder = folders.get(normalized)

    if folder is None:
        return {"success": False, "error": f"'{folder_name}' is not an approved folder."}

    if not folder.exists():
        return {"success": False, "error": f"The folder does not exist: {folder}"}

    try:
        system().open_path(folder)
    except OSError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "message": f"Opening {normalized}.", "path": str(folder)}
