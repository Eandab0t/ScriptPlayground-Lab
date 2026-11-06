@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -c "import aiohttp, discord" 2>nul
if errorlevel 1 (
  echo Python 3.11+ with the project dependencies was not found.
  echo Fix: install Python from https://www.python.org/downloads/ and run:
  echo     python -m pip install -r "%~dp0requirements.txt"
  echo Then start this file again.
  pause
  exit /b 1
)
python launcher.py
