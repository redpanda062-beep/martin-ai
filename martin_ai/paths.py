from __future__ import annotations

import os
from pathlib import Path

from .platform import is_macos


PROJECT_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    override = os.getenv("MARTIN_AI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "MartinAI"
    # Preserve the existing Windows location exactly.
    return Path.home() / "Documents" / "MartinAI"


DATA_DIR = _default_data_dir()
LOCAL_CONFIG_PATH = DATA_DIR / "settings.json"
DATABASE_PATH = DATA_DIR / "martin_ai.db"
PAIRING_PATH = DATA_DIR / "phone_remote_devices.json"
