from __future__ import annotations

from .config import load_settings
from .core import PermissionManager, RegisteredTool, RuntimeControl, ToolRegistry
from .memory import MemoryManager
from .models import ActionRequest
from .tools import READ_ONLY_TOOLS, REPAIR_TOOLS, TOOL_METADATA
from .self_repair import SelfRepairEngine, repair_registry
from .skills import SkillManager
from .config import APP_DIR
from .crash_detective import configure_crash_detective


settings = load_settings()
memory_manager = MemoryManager()
crash_detective = configure_crash_detective(
    memory_manager, enabled=bool(settings.get("crash_detective", {}).get("enabled", True)))
runtime_control = RuntimeControl(simulation_mode=bool(settings["simulation_mode"]),
                                 autonomy_enabled=bool(settings["autonomy_enabled"]))
permission_manager = PermissionManager(int(settings["permission_level"]))
tool_registry = ToolRegistry(permission_manager, runtime_control, memory_manager,
                             maximum_actions_per_hour=int(settings["agent"]["maximum_actions_per_hour"]))
self_repair_engine = SelfRepairEngine(
    repair_registry, runtime_control, memory_manager,
    enabled=bool(settings.get("self_repair", {}).get("enabled", True)),
    auto_level_1=bool(settings.get("self_repair", {}).get("auto_level_1", False)),
)

for _name, _function in {**READ_ONLY_TOOLS, **REPAIR_TOOLS}.items():
    _metadata = TOOL_METADATA[_name]
    _changes = _name in REPAIR_TOOLS or _metadata["risk_level"] == "limited"
    _required = 3 if _name in REPAIR_TOOLS else (2 if _changes else 1)
    tool_registry.register(RegisteredTool(
        name=_name, description=_metadata["description"], risk_level=_metadata["risk_level"],
        required_permission_level=_required, function=_function, timeout=_metadata["timeout"],
        changes_system=_changes,
    ))


def execute_registered(name: str, arguments: dict, *, approved: bool = False,
                       trigger: str = "application") -> dict:
    return tool_registry.execute(ActionRequest(name, arguments, trigger=trigger),
                                 explicitly_approved=approved)


skill_manager = SkillManager(
    lambda name, arguments, approved=False: execute_registered(
        name, arguments, approved=approved, trigger="skill"),
    enabled=settings.get("skills", {}).get("enabled", {}),
    activity_path=APP_DIR / "skill-activity.jsonl",
)
skill_manager.discover()
skill_manager.load_all()
