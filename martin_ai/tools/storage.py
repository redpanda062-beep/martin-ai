from __future__ import annotations

import os
import stat
import time
from collections import deque
from pathlib import Path
from typing import Any


def _entries(root: Path):
    """Read metadata only; never follow junctions, links, or cloud placeholders."""
    pending = deque([root])
    while pending:
        directory = pending.popleft()
        try:
            info = directory.stat(follow_symlinks=False)
            if _excluded(info):
                yield None
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _excluded(info):
                            yield None
                        elif stat.S_ISDIR(info.st_mode):
                            pending.append(Path(entry.path))
                            yield (None, 0)
                        elif stat.S_ISREG(info.st_mode):
                            yield (entry.path, info.st_size)
                    except OSError:
                        yield None
        except OSError:
            yield None


def _excluded(info) -> bool:
    # REPARSE_POINT, OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS.
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                            (0x400 | 0x1000 | 0x40000 | 0x400000))


def _scan_locations(locations: list[tuple[str, Path]], *, max_entries: int = 30000,
                    seconds: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    rows = [{"name": name, "path": str(path), "bytes": 0, "files": 0,
             "skipped": 0, "complete": False} for name, path in locations]
    active = deque((row, _entries(path)) for row, (_, path) in zip(rows, locations))
    largest: list[dict[str, Any]] = []
    examined = 0
    try:
        while active and examined < max_entries and time.monotonic() < deadline:
            row, iterator = active.popleft()
            try:
                item = next(iterator)
            except StopIteration:
                row["complete"] = row["skipped"] == 0
                continue
            active.append((row, iterator))
            examined += 1
            if item is None:
                row["skipped"] += 1
            elif item[0] is not None:
                path, size = item
                row["bytes"] += size
                row["files"] += 1
                largest.append({"path": path, "bytes": size, "location": row["name"]})
                largest.sort(key=lambda file: file["bytes"], reverse=True)
                del largest[10:]
    finally:
        for _, iterator in active:
            iterator.close()
    return {"success": True, "changes_made": False,
            "scope": "Selected user folders and Windows temporary files; not a whole-drive scan.",
            "size_basis": "Logical file sizes, not exact allocated disk space; cloud placeholders and links are skipped.",
            "partial": any(not row["complete"] for row in rows), "entries_examined": examined,
            "locations": sorted(rows, key=lambda row: row["bytes"], reverse=True),
            "largest_files": largest,
            "suggestions": [
                "Review Downloads and large personal files; preserve projects and backups before moving anything.",
                "Use Windows Settings > System > Storage > Temporary files to review cleanup choices.",
                "Uninstall unused apps through Windows Settings rather than deleting application folders.",
            ]}


def investigate_storage() -> dict[str, Any]:
    home = Path.home()
    locations = [(name, home / name) for name in
                 ("Downloads", "Documents", "Desktop", "Videos", "Pictures", "Music")]
    locations += [("AppData Local", home / "AppData" / "Local"),
                  ("AppData Roaming", home / "AppData" / "Roaming"),
                  ("AppData LocalLow", home / "AppData" / "LocalLow")]
    if os.name == "nt":
        locations.append(("Windows temporary files", Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"))
    return _scan_locations([(name, path) for name, path in locations if path.is_dir()])
