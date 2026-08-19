from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Alert:
    key: str
    title: str
    message: str
    severity: str
    value: float | None = None
    shown: bool = False


class AlertManager:
    """Owns autonomous alert state; chat routing never reads this implicitly."""

    def __init__(self) -> None:
        self.active: dict[str, Alert] = {}

    def update_system_sample(self, sample: dict[str, Any]) -> list[Alert]:
        changed: list[Alert] = []
        current_keys: set[str] = set()
        for disk in sample.get("disks", []):
            free = float(disk.get("free_gb", 9999))
            if free >= 10:
                continue
            device = str(disk.get("device") or "drive")
            key = f"storage-low:{device.lower()}"
            current_keys.add(key)
            previous = self.active.get(key)
            significantly_worse = previous is not None and previous.value is not None and free <= previous.value - 2
            if previous is None or significantly_worse:
                alert = Alert(key, "Storage Warning", f"{device} has about {free:g} GB free.", "Medium", free)
                self.active[key] = alert
                changed.append(alert)
        for key in [key for key in self.active if key.startswith("storage-low:") and key not in current_keys]:
            del self.active[key]
        return changed

    def mark_shown(self, key: str) -> None:
        if key in self.active:
            self.active[key].shown = True


alert_manager = AlertManager()
