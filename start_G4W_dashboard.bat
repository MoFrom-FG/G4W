@echo off
setlocal

set "PORTABLE_ROOT=%~dp0"
set "PYTHON=%PORTABLE_ROOT%runtime\app\.venv\Scripts\python.exe"
set "GA_APP_DIR=%PORTABLE_ROOT%runtime\app"
set "G4W_HOME=%PORTABLE_ROOT%runtime\G4W-main"
set "G4W_STATE_DIR=%PORTABLE_ROOT%runtime\G4W-data"
set "G4W_WORKSPACE_ROOT=%PORTABLE_ROOT%"
set "PYTHONPATH=%G4W_HOME%;%GA_APP_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

if not exist "%PYTHON%" (
  echo [G4W] Prepared Python not found. Run 1_prepare_G4W_ga.bat first.
  pause
  exit /b 1
)

cd /d "%G4W_HOME%"
echo [G4W] Starting local dashboard at http://127.0.0.1:18180
start "G4W Dashboard" http://127.0.0.1:18180
"%PYTHON%" -B -u -m G4W.dashboard.server --host 127.0.0.1 --port 18180
