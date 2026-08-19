@echo off
title Martin AI Desktop
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo Run setup first.
  pause
  exit /b 1
)
start "Martin AI" .venv\Scripts\pythonw.exe -m martin_ai.gui_app
