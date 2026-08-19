@echo off
title Martin AI Local Setup
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON=py"
  goto found
)

where python >nul 2>nul
if not errorlevel 1 (
  set "PYTHON=python"
  goto found
)

echo Python was not found.
echo Install Python, then run this setup again.
pause
exit /b 1

:found
%PYTHON% -m venv .venv
if errorlevel 1 (
  echo Failed to create the Python environment.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python -c "import PySide6" || (
  echo PySide6 installation failed.
  pause
  exit /b 1
)

if not exist .env copy .env.example .env >nul

python -m martin_ai.remote_background install

echo.
echo Setup complete.
echo Martin Remote Agent was registered to start after this Windows user logs in.
echo Make sure Ollama is installed and qwen3:8b has been downloaded.
pause
