from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .base import Skill, SkillMatch, SkillStatus
from .builtin import BUILTIN_SKILLS


class SkillValidationError(ValueError): pass
class DuplicateSkillError(ValueError): pass


class SkillManager:
    def __init__(self, executor: Callable[[str, dict[str, Any], bool], dict[str, Any]], *,
                 enabled: dict[str, bool] | None = None, activity_path: Path | None = None) -> None:
        self.executor = executor
        self.enabled = dict(enabled or {})
        self.skills: dict[str, Skill] = {}
        self.activity_path = activity_path
        self.activity: list[dict[str, Any]] = []

    @staticmethod
    def validate(skill: Skill) -> None:
        meta = skill.metadata
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", meta.id):
            raise SkillValidationError("Invalid skill id")
        if not all((meta.name, meta.description, meta.version, meta.author)):
            raise SkillValidationError("Required skill metadata is missing")
        ids = [item.id for item in meta.capabilities]
        if len(ids) != len(set(ids)) or any(not item for item in ids):
            raise SkillValidationError("Capability ids must be unique and non-empty")
        if any(level not in range(1, 5) for level in meta.permissions):
            raise SkillValidationError("Permission levels must be between 1 and 4")

    def discover(self, classes: Iterable[type[Skill]] = BUILTIN_SKILLS) -> None:
        for skill_class in classes:
            skill = skill_class(self.executor)
            self.validate(skill)
            if skill.metadata.id in self.skills:
                raise DuplicateSkillError(skill.metadata.id)
            self.skills[skill.metadata.id] = skill

    def load_all(self) -> None:
        for skill in self.skills.values():
            if not self.enabled.get(skill.metadata.id, True):
                skill._status = SkillStatus.DISABLED
                continue
            missing = [dep for dep in skill.metadata.dependencies if dep not in self.skills or
                       self.skills[dep]._status in {SkillStatus.DISABLED, SkillStatus.ERROR, SkillStatus.UNAVAILABLE}]
            try:
                skill.initialize()
                if missing:
                    skill._status, skill._status_detail = SkillStatus.DEGRADED, "Missing: " + ", ".join(missing)
                    self.log("dependency_failure", skill.metadata.id, detail=skill._status_detail)
                else:
                    self.log("skill_loaded", skill.metadata.id)
            except Exception as exc:
                skill._status, skill._status_detail = SkillStatus.ERROR, str(exc)
                logging.exception("skill_load_failed skill=%s", skill.metadata.id)
                self.log("skill_failure", skill.metadata.id, detail="Initialization failed")

    def set_enabled(self, skill_id: str, enabled: bool) -> bool:
        skill = self.skills.get(skill_id)
        if not skill: return False
        self.enabled[skill_id] = enabled
        if enabled:
            try: skill.initialize()
            except Exception:
                skill._status = SkillStatus.ERROR
                return False
        else: skill.shutdown()
        self.log("skill_loaded" if enabled else "skill_unloaded", skill_id)
        return True

    def route(self, request: str) -> SkillMatch | None:
        matches = []
        for skill in self.skills.values():
            if skill._status not in {SkillStatus.HEALTHY, SkillStatus.DEGRADED}: continue
            try:
                match = skill.can_handle(request)
                if match:
                    health = .95 if skill._status == SkillStatus.HEALTHY else .7
                    risk = min(c.permission_level for c in skill.metadata.capabilities if c.id == match.capability)
                    match.confidence = match.confidence * health - (risk - 1) * .03
                    matches.append(match)
            except Exception:
                skill._status = SkillStatus.DEGRADED
                logging.exception("skill_route_failed skill=%s", skill.metadata.id)
        return max(matches, key=lambda item: item.confidence, default=None)

    def execute(self, match: SkillMatch, *, approved: bool = False) -> dict[str, Any]:
        skill = self.skills.get(match.skill_id)
        if not skill or skill._status == SkillStatus.DISABLED:
            return {"success": False, "error": "That skill is disabled or unavailable."}
        self.log("skill_selected", match.skill_id, capability=match.capability)
        try:
            result = skill.execute(match.capability, match.parameters, approved=approved)
            event = "capability_executed" if result.get("success") else ("permission_requested" if result.get("approval_required") else "permission_denied")
            self.log(event, match.skill_id, capability=match.capability)
            return result
        except Exception:
            skill._status, skill._status_detail = SkillStatus.DEGRADED, "Last execution failed"
            logging.exception("skill_execution_failed skill=%s capability=%s", match.skill_id, match.capability)
            self.log("skill_failure", match.skill_id, capability=match.capability, detail="Execution failed")
            return {"success": False, "error": "That skill had a problem. Martin AI is still running."}

    def log(self, event: str, skill_id: str, **fields: Any) -> None:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, "skill_id": skill_id,
               **{key: value for key, value in fields.items() if key not in {"password", "token", "secret"}}}
        self.activity.append(row); self.activity = self.activity[-500:]
        logging.info("skill_event %s", json.dumps(row, sort_keys=True))
        if self.activity_path:
            try:
                self.activity_path.parent.mkdir(parents=True, exist_ok=True)
                with self.activity_path.open("a", encoding="utf-8") as stream: stream.write(json.dumps(row) + "\n")
            except OSError: logging.exception("skill_activity_write_failed")

    def information(self) -> list[dict[str, Any]]:
        return [{**asdict(skill.metadata), "enabled": skill._status != SkillStatus.DISABLED,
                 **skill.get_status()} for skill in self.skills.values()]

