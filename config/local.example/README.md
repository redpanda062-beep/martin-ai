# Per-machine configuration example

Copy this directory to `config/local/` only if a developer override is needed.
The real directory is ignored by Git. Runtime state normally lives outside the
repository:

- Windows: `%USERPROFILE%\Documents\MartinAI`
- macOS: `~/Library/Application Support/MartinAI`
- Override: set `MARTIN_AI_DATA_DIR` to an absolute local directory

Never commit `.env`, device pairing credentials, local databases, logs, model
caches, certificates, or paths that only exist on one computer.
