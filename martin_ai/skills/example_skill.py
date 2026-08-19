"""Copy-only example. It is intentionally not installed or activated."""
from .base import Capability, Skill, SkillMatch, SkillMetadata


class ExampleStatusSkill(Skill):
    metadata = SkillMetadata(
        id="example_status", name="Example Status", description="Reads Martin's system summary.",
        version="0.1.0", author="Your name",
        capabilities=(Capability("system_summary", "Read system information", 1,
                                 keywords=("example status",)),),
        supported_intents=("example_status",), permissions=(1,), risk_level="read_only",
        dependencies=("system_control",),
    )

    def can_handle(self, request: str):
        return SkillMatch(self.metadata.id, "system_summary", .9) if "example status" in request.lower() else None

    def execute(self, action, parameters, *, approved=False):
        return self._executor("system_summary", {}, approved)
