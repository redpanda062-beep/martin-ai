@echo off
title Martin Remote Agent Setup
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m martin_ai.remote_background install
.venv\Scripts\python.exe -m martin_ai.remote_background start
echo.
echo Martin Remote Agent is registered for this Windows user and is starting now.
pause
