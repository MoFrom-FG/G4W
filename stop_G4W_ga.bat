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
echo [G4W] Stopping Conductor and its Worker child processes...
"%PYTHON%" -m G4W stop
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo [G4W] Stop failed with exit code %RESULT%.
if "%RESULT%"=="0" echo [G4W] Stop signal completed. Service/model terminals will show their exit status.
if /i "%~1"=="quiet" exit /b %RESULT%
pause
exit /b %RESULT%
