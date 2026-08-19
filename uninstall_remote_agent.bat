@echo off
title Remove Martin Remote Agent
cd /d "%~dp0"
.venv\Scripts\python.exe -m martin_ai.remote_background stop
.venv\Scripts\python.exe -m martin_ai.remote_background uninstall
echo Martin Remote Agent startup registration was removed.
pause
