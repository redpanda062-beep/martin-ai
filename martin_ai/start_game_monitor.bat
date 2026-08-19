@echo off
title Martin AI Game Monitor
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -u -m martin_ai.game_monitor
) else (
    python -u -m martin_ai.game_monitor
)
pause
