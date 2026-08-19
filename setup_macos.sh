#!/bin/sh
set -eu

cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
printf '%s\n' 'Setup complete. Install Ollama separately, then run: ./start_macos.sh'
