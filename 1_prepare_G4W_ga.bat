@echo off
setlocal
chcp 65001 >nul

set "PORTABLE_ROOT=%~dp0"
set "BASE_PYTHON=%PORTABLE_ROOT%runtime\python\python.exe"
set "GA_APP_DIR=%PORTABLE_ROOT%runtime\app"
set "VENV_DIR=%GA_APP_DIR%\.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"
set "WHEELS=%PORTABLE_ROOT%runtime\wheels"
set "G4W_HOME=%PORTABLE_ROOT%runtime\G4W-main"
set "G4W_STATE_DIR=%PORTABLE_ROOT%runtime\G4W-data"
set "G4W_WORKSPACE_ROOT=%PORTABLE_ROOT%"
set "PYTHONPATH=%G4W_HOME%;%GA_APP_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "TEMP=%PORTABLE_ROOT%runtime\temp"
set "TMP=%TEMP%"

if not exist "%BASE_PYTHON%" (
  echo [G4W] Portable Python not found: %BASE_PYTHON%
  pause
  exit /b 1
)
if not exist "%TEMP%" mkdir "%TEMP%"

if not exist "%PYTHON%" (
  echo [G4W] Creating the package-local GA 1.8 environment...
  "%BASE_PYTHON%" -m venv "%VENV_DIR%"
  if errorlevel 1 goto failed
)

echo [G4W] Installing bundled GA dependencies from offline wheels...
"%PYTHON%" -m pip install --disable-pip-version-check --no-index --find-links "%WHEELS%" "requests>=2.28" "beautifulsoup4>=4.12" "bottle>=0.12" "simple-websocket-server>=0.4" "aiohttp>=3.9" psutil
if errorlevel 1 goto failed

cd /d "%G4W_HOME%"
echo [G4W] Checking and preparing G4W...
"%PYTHON%" -m G4W prepare
if errorlevel 1 goto failed

echo.
echo [G4W] Portable package is ready.
echo [G4W] Next: 2_key_for_ga.bat
pause
exit /b 0

:failed
echo.
echo [G4W] Preparation failed with exit code %ERRORLEVEL%.
pause
exit /b 1
