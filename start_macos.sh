#!/bin/sh
set -eu

cd "$(dirname "$0")"
. .venv/bin/activate
python -m martin_ai.gui_app
