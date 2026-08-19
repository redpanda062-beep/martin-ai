@echo off
title Martin AI Local
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup first.
  pause
  exit /b 1
)
start "Martin AI Game Monitor" /min .venv\Scripts\python.exe -u -m martin_ai.game_monitor
.venv\Scripts\python.exe main.py
pause
