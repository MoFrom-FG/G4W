@echo off

setlocal

set "ROOT=%~dp0..\"

set "PY=%ROOT%runtime\python\python.exe"

if not exist "%PY%" set "PY=%ROOT%.venv\Scripts\python.exe"

if not exist "%PY%" (

  echo [ERROR] Portable Python not found.

  exit /b 2

)

"%PY%" -B "%ROOT%tools\generate_browser_index.py" "%ROOT%runtime\G4W-data\short-path-mirror" "%ROOT%runtime\G4W-data\short-path-mirror\browse"

set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" echo [ERROR] Browse generation failed with code %RC%.

exit /b %RC%

