# Cross-platform architecture

Martin AI uses one repository for shared core, brain, skills, memory, models, UI,
and Ollama integration. `martin_ai.platform.runtime` detects the host with
Python's standard platform API and lazily imports exactly one backend.

Platform-neutral code calls `martin_ai.platform.system()` instead of importing
Win32 or macOS APIs. New native features belong under `platform/windows/` or
`platform/macos/` and should implement a shared protocol from `platform/base.py`.
Unsupported native capabilities must return a clear unavailable result rather
than attempting a command from the wrong operating system.

Machine data is deliberately outside Git. `MARTIN_AI_DATA_DIR` can override the
default data directory. Never point two simultaneously running computers at the
same SQLite database; sync source through Git, not live runtime state.
