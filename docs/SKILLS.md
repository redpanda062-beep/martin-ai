# Martin AI skill system

Martin AI remains the orchestrator. A skill describes and selects capabilities; only the existing registered-tool boundary can execute them. This prevents a skill from receiving unrestricted access to the application or Windows.

## Lifecycle

`SkillManager.discover()` validates local built-ins and rejects invalid or duplicate IDs. `load_all()` initializes enabled skills and checks dependencies. A skill moves through Starting, Healthy, Disabled, Degraded, Error, or Unavailable. Exceptions are contained, recorded without request parameters or secrets, and do not stop other skills. `shutdown()` is called when a skill is disabled.

Settings are stored in the existing Martin settings document under `skills.enabled`. Activity uses JSON Lines at `Documents/MartinAI/skill-activity.jsonl` and is capped in memory. Martin records event type, skill, capability, and timestamp—not arguments, tokens, pairing secrets, or response data.

## Routing and permissions

Each capability declares intent/keyword hints and a permission level. The router ranks matches by keyword confidence, health, and risk. Execution then goes through `ToolRegistry`, which applies Martin's existing permission manager, argument validation, rate limit, simulation mode, confirmation, and audit memory.

- Level 1: read-only; automatic.
- Level 2: safe action; allowed only at the configured autonomy level.
- Level 3: confirmation required.
- Level 4: restricted/administrator; always explicitly approved and never appropriate for protection-weakening actions.

Skills cannot register arbitrary shell text. Security tools do not expose actions that disable Defender, Firewall, SmartScreen, antivirus, or security services.

## Creating a built-in skill

Copy `martin_ai/skills/example_skill.py`, give it a unique lowercase ID, declare narrow capabilities, and add its class to `BUILTIN_SKILLS`. The capability ID must already exist in Martin's approved tool registry. Add routing, permission, failure, and dependency tests. Generated or downloaded code must remain inactive until it is validated, tested, reviewed for permissions/hash, and explicitly installed and enabled by the user.

Dependencies are skill IDs in `SkillMetadata.dependencies`. Missing or unhealthy dependencies make a skill Degraded instead of crashing startup.

## Crash Detective

The Gaming skill exposes `analyze_crash`, `get_last_crash`, `get_crash_history`, and `explain_crash` as Level 1 capabilities. Game Mode sends unexpected process termination, bounded telemetry, and its already-collected Windows events to Crash Detective. A separate 30-second throttled watcher reads only recent Application Error, Windows Error Reporting, Display, and NVIDIA-related event IDs for ordinary applications.

Evidence is accepted only within 90 seconds of the crash. Direct application matches, WER/Application Error records, GPU resets, explicit memory failures, and fault signatures receive stronger weights. Old or generic warnings receive no causal weight. Incidents are stored in Martin's existing `incidents` table and paths shown to users replace the home directory with `%USERPROFILE%`.

Run all tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
