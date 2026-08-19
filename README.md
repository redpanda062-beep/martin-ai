# Martin AI Local

The same repository is designed to run on Windows and macOS. Shared core, UI,
skills, memory, and Ollama integration live under `martin_ai/`; native services
are selected at runtime from `martin_ai/platform/windows` or
`martin_ai/platform/macos`. See `docs/CROSS_PLATFORM.md`.

This version uses Ollama and `qwen3:8b` on your PC. It does not require an OpenAI API key.

## Before starting

1. Install Ollama.
2. Confirm this works in Command Prompt:

   ollama run qwen3:8b

3. Type `/bye` to leave the Ollama chat.

## Installation

1. Extract this ZIP.
2. Open the `MartinAI-Local` folder.
3. Double-click `setup`.
4. When setup finishes, double-click `start`.

## Gaming performance

Martin AI asks Ollama to unload the model after every completed response. This frees the model's VRAM instead of leaving it loaded for five minutes.

Starting Martin also starts Game Mode in a minimized window. Game Mode detects
the games listed in `martin_ai/games.json`, unloads Ollama when a game starts,
and records CPU, RAM, GPU, temperature, and VRAM statistics. Monitoring is
read-only: it does not close applications or change the system. Any protected
optimization must be requested through Martin AI and explicitly approved.

When the game closes, Game Mode automatically checks Windows crash events and
the recorded CPU, RAM, disk, GPU, VRAM, and temperature data. It saves a
deterministic diagnosis plus a concise local-AI explanation in the session
report. Troubleshooting and recommendations are automatic, while repairs
require explicit approval.

## Protected game repairs

Game Mode can identify graphics-specific crash signatures (for example,
Direct3D, DXGI, or an NVIDIA display-driver fault). Clearing shader caches and
all other repairs remain protected actions and require explicit approval.

You may also type:

`unload`

to force the model out of memory.

## Example requests

- Check my system and tell me why it feels slow.
- Check recent application errors.
- Why did my game crash?
- Check my NVIDIA driver and temperature.
- Clear my shader caches.
- Save a diagnostic report.
- Find a file in my Downloads folder.
- Take a screenshot.
- List running apps.
- Read text from my clipboard.

Repairs require you to type `APPROVE`.

## AI investigations

Natural-language PC problems now open a bounded investigation. Martin builds a
visible 3-8 step plan, calls only registered tools, runs read-only diagnostics
in a worker thread, and pauses before every protected action. The same plan
resumes after approval or denial. Completed JSON and Markdown reports are saved
under `Documents\MartinAI Investigations`.

The terminal launcher remains `start.bat`. The red-and-black desktop launcher
remains `start_gui.bat`; its Investigation page shows the plan, live evidence,
approval state, diagnosis, confidence, recommendations, and sensor errors.

## Built-in safety

Martin only opens applications from its approved list. Actions that close apps,
change caches, run Windows repairs, or replace clipboard text wait for explicit
approval. Type `pending`, `approve`, or `deny` to manage the waiting action.

## Jarvis safety architecture

The desktop application now starts in Simulation Mode on first run. The AI can
observe, analyze, plan, and write audit records, but tools marked as changing
the PC report what they would do instead of executing. Use Settings to change
the permission level or simulation setting. The top bar shows the active level,
an autonomy toggle, and a large STOP AI control. STOP AI cancels pending work
and prevents any new registered action while passive telemetry remains visible.

Persistent state is stored in `Documents\MartinAI\martin_ai.db`. It contains
incidents, observations, actions and results, known fixes, game profiles, user
preferences, hardware profiles, application history, and the audit trail.
Passwords, tokens, cookies, and credential-like fields are filtered from stored
JSON. Configuration lives in `Documents\MartinAI\settings.json`.

All model-requested operations cross the controlled registry in
`martin_ai/core/tool_registry.py`; unknown tools and command/script arguments
are rejected. Permission Levels 3 and 4 always pause for explicit approval,
and Level 4 can never be automatically approved. Shader-cache cleanup now moves
files to a Martin AI rollback folder instead of permanently deleting them.

The modular autonomous foundation is under `martin_ai/core`, perception under
`martin_ai/monitoring`, data models under `martin_ai/models`, and SQLite memory
under `martin_ai/memory`. Sensor failures are isolated so one unavailable GPU,
temperature, foreground-window, or network sensor does not stop monitoring.
