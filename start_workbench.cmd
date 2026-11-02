@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found. See WEBUI_README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m workbench.server
if errorlevel 1 pause
