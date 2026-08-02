@echo off
setlocal
chcp 65001 >nul

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
if not exist "%G4W_HOME%\.env" (
  echo [G4W] Package-local ENV has not been initialized.
  echo [G4W] Run 3_env_for_G4W.bat before logging in.
  pause
  exit /b 1
)

cd /d "%G4W_HOME%"
echo [G4W] Opening the WeChat test-account QR login...
echo [G4W] Keep this window open until login succeeds.
"%PYTHON%" -u -m G4W login
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo [G4W] Login failed with exit code %RESULT%.
pause
exit /b %RESULT%
