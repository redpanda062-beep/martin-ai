from __future__ import annotations

import os
import subprocess
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .diagnostics import _run
from ..windows_process import hidden_process_kwargs


def flush_dns() -> dict[str, Any]:
    return _run(["ipconfig.exe", "/flushdns"], timeout=20)


def reset_microsoft_store_cache() -> dict[str, Any]:
    try:
        subprocess.Popen(["wsreset.exe"], **hidden_process_kwargs())
        return {"success": True, "message": "Microsoft Store reset started."}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def clear_shader_caches() -> dict[str, Any]:
    targets = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "D3DSCache",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA" / "DXCache",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA" / "GLCache",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NVIDIA Corporation" / "NV_Cache",
    ]

    quarantine = Path.home() / "Documents" / "MartinAI" / "rollback" / f"shader-cache-{datetime.now():%Y%m%d-%H%M%S-%f}"
    moved_files = 0
    failures = []

    for target in targets:
        if not target.exists():
            continue
        for child in target.rglob("*"):
            if child.is_file():
                try:
                    relative = Path(target.name) / child.relative_to(target)
                    destination = quarantine / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(child), str(destination))
                    moved_files += 1
                except Exception as exc:
                    failures.append(f"{child}: {exc}")

    return {
        "success": not failures,
        "deleted_files": 0,
        "moved_files": moved_files,
        "failed_count": len(failures),
        "failed_examples": failures[:10],
        "note": "Games may stutter temporarily while shaders rebuild.",
        "rollback": {"available": moved_files > 0, "quarantine_path": str(quarantine),
                     "method": "restore_previous_setting"},
    }


def restore_shader_caches(quarantine_path: str) -> dict[str, Any]:
    base = Path.home() / "Documents" / "MartinAI" / "rollback"
    source = Path(quarantine_path).resolve(strict=False)
    try:
        source.relative_to(base.resolve(strict=False))
    except ValueError:
        return {"success": False, "error": "Rollback source is outside the allowlisted rollback folder."}
    if not source.is_dir():
        return {"success": False, "error": "Rollback data does not exist."}
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    restored = 0
    for cache_folder in source.iterdir():
        target = local / cache_folder.name
        for item in cache_folder.rglob("*"):
            if item.is_file():
                destination = target / item.relative_to(cache_folder)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(destination))
                restored += 1
    return {"success": True, "restored_files": restored}


def run_sfc_scan() -> dict[str, Any]:
    return _run(["sfc.exe", "/scannow"], timeout=1800)


def run_dism_restorehealth() -> dict[str, Any]:
    return _run(["dism.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"], timeout=1800)
