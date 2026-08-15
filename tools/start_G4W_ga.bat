@echo off
setlocal

set "PORTABLE_ROOT=%~dp0..\"
set "PYTHON=%PORTABLE_ROOT%runtime\app\.venv\Scripts\python.exe"
set "GA_APP_DIR=%PORTABLE_ROOT%runtime\app"
set "G4W_HOME=%PORTABLE_ROOT%runtime\G4W-main"
set "G4W_STATE_DIR=%PORTABLE_ROOT%runtime\G4W-data"
set "G4W_WORKSPACE_ROOT=%PORTABLE_ROOT%"
set "PYTHONPATH=%G4W_HOME%;%GA_APP_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

if /i "%~1"=="service" goto run_service
if /i "%~1"=="model" goto run_model

if not exist "%PYTHON%" (
  echo [G4W] Prepared Python not found. Run 1_prepare_G4W_ga.bat first.
  pause
  exit /b 1
)
if not exist "%G4W_HOME%\G4W\__main__.py" (
  echo [G4W] Python G4W not found: %G4W_HOME%\G4W
  pause
  exit /b 1
)
if not exist "%G4W_HOME%\.env" (
  echo [G4W] Package-local ENV has not been initialized.
  echo [G4W] Run 3_env_for_G4W.bat first.
  pause
  exit /b 1
)

"%PYTHON%" -m G4W sync-runtime-paths >nul
if errorlevel 1 (
  echo [G4W] Failed to refresh G4W runtime paths in package-local ENV.
  pause
  exit /b 1
)

call "%PORTABLE_ROOT%stop_G4W_ga.bat" quiet

where wt.exe >nul 2>nul
if not errorlevel 1 (
  echo [G4W] Opening GA/service logs and model thinking in Windows Terminal...
  wt new-tab --title "G4W GA Logs" powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -Command "& '%~f0' service" ; split-pane -H --title "G4W Model Thinking" powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -Command "& '%~f0' model"
  exit /b 0
)

echo [G4W] Windows Terminal not found; opening two PowerShell windows...
start "G4W GA Logs" powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -Command "& '%~f0' service"
start "G4W Model Thinking" powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -Command "& '%~f0' model"
exit /b 0

:run_service
cd /d "%G4W_HOME%"
rem Inject EMBEDDING_* / G4W_VECTOR_* from package-local .env into process env
rem (embedding.py only reads os.environ; optional 5_embedding_for_G4W.bat writes them)
if exist "%PORTABLE_ROOT%tools\inject_embedding_env.py" (
  rem CMD for /f + backticks needs ""exe" "arg"" quoting or paths parse as invalid
  for /f "usebackq delims=" %%L in (`""%PYTHON%" "%PORTABLE_ROOT%tools\inject_embedding_env.py""`) do %%L
)
echo [G4W GA Logs] Starting Python Conductor and Worker control plane...
"%PYTHON%" -u -m G4W start
set "RESULT=%ERRORLEVEL%"
echo.
if exist "%G4W_STATE_DIR%\G4W.stop-requested" (
  set "RESULT=0"
  echo [G4W GA Logs] G4W/GA was stopped by stop_G4W_ga.bat and has exited normally.
) else (
  echo [G4W GA Logs] G4W/GA process exited with code %RESULT%.
)
echo [G4W GA Logs] This terminal is intentionally kept open for inspection.
exit /b %RESULT%

:run_model
cd /d "%G4W_HOME%"
echo [G4W Model Monitor] Starting...
"%PYTHON%" -u -m G4W monitor
set "RESULT=%ERRORLEVEL%"
echo.
echo [G4W Model Monitor] Model output monitor exited with code %RESULT%. This terminal is intentionally kept open.
exit /b %RESULT%
