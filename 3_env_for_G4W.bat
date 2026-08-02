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
cd /d "%G4W_HOME%"
echo [G4W] Initializing the package-local G4W ENV...
"%PYTHON%" -m G4W init-env
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" (
  echo.
  echo [G4W] G4W ENV is ready and remains portable after moving the whole folder.
  echo [G4W] Next: 4_login_G4W_ga.bat
)
pause
exit /b %RESULT%
