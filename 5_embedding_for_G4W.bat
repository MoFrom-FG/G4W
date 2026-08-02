@echo off
setlocal EnableExtensions
REM Portable entry: ST thin-HTTP vector addon (NOT LM Studio .env menu)
REM Target: runtime\G4W-embedding  |  gate: WeChat /vector on|off
REM NOTE: keep this file ASCII-safe for cmd.exe (avoid fullwidth punctuation)

set "PORTABLE_ROOT=%~dp0"
if "%PORTABLE_ROOT:~-1%"=="\" set "PORTABLE_ROOT=%PORTABLE_ROOT:~0,-1%"
set "G4W_HOME=%PORTABLE_ROOT%\runtime\G4W-main"
set "EMB_ROOT=%PORTABLE_ROOT%\runtime\G4W-embedding"
set "GA_APP_DIR=%PORTABLE_ROOT%\runtime\app"
set "G4W_STATE_DIR=%PORTABLE_ROOT%\runtime\G4W-data"
set "G4W_WORKSPACE_ROOT=%PORTABLE_ROOT%"
set "PYTHONPATH=%G4W_HOME%;%GA_APP_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_XET=1"

REM CLI: prefer GA .venv; bare portable python also OK for install_embedding.py
REM ensure_venv still uses portable base python to create embedding .venv
set "PY="
if exist "%GA_APP_DIR%\.venv\Scripts\python.exe" set "PY=%GA_APP_DIR%\.venv\Scripts\python.exe"
if not defined PY if exist "%PORTABLE_ROOT%\runtime\python\python.exe" set "PY=%PORTABLE_ROOT%\runtime\python\python.exe"
if not defined PY set "PY=python"

if not exist "%G4W_HOME%\G4W\memory\vector\install_embedding.py" (
  echo [G4W] install_embedding missing under:
  echo   %G4W_HOME%
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  G4W Embedding addon install
echo  Sentence-Transformers + thin HTTP + Qwen3-Embedding-0.6B
echo ============================================================
echo  code:    %G4W_HOME%
echo  addon:   %EMB_ROOT%
echo  Python:  %PY%
echo  size:    about 4-8 GB first time (GPU Torch + ST + model)
echo.
echo  Notes (NOT the old LM Studio 0-7 menu):
echo    - installs into G4W-embedding\.venv (does not pollute GA)
echo    - downloads Qwen model automatically: hf-mirror.com, then official fallback
echo    - interrupted model downloads resume when this script is run again
echo    - NVIDIA GPU detected: installs CUDA 12.8 Torch; otherwise CPU Torch
echo    - PyTorch download: NJU mirror first, then Aliyun and official fallback
echo    - may request UAC once to install the official Microsoft VC++ runtime for Torch
echo    - does NOT enable the product gate (use WeChat /vector on)
echo    - does NOT start a long-lived embed daemon; lifecycle starts on /vector on
echo    - default port 8081 (avoid CPA/8080)
echo    - old options 1-7 (.env / LM Studio) are retired from this bat
echo ============================================================
echo.
if defined G4W_HF_ENDPOINT (
  echo    - custom model endpoint: %G4W_HF_ENDPOINT%
) else if defined HF_ENDPOINT (
  echo    - custom model endpoint: %HF_ENDPOINT%
) else (
  echo    - model endpoint: hf-mirror.com -^> huggingface.co fallback
)
if defined G4W_TORCH_MODE (
  echo    - Torch mode override: %G4W_TORCH_MODE%
) else (
  echo    - Torch mode: auto ^(NVIDIA GPU -^> CUDA, no GPU -^> CPU^)
)
echo ============================================================
echo.
set /p "ANS=Continue full install? [y/N]: "
if /i not "%ANS%"=="y" if /i not "%ANS%"=="yes" (
  echo Cancelled.
  pause
  exit /b 1
)

cd /d "%G4W_HOME%"
set "INSTALL_PY=%G4W_HOME%\G4W\memory\vector\install_embedding.py"
REM Run .py directly; avoid python -m pulling vector/__init__ -^> numpy on bare python

echo.
echo [1/3] dry-run preview...
"%PY%" -u "%INSTALL_PY%" --dry-run --yes --root "%EMB_ROOT%"
if errorlevel 1 (
  echo dry-run failed. Check install_embedding.py / Python.
  pause
  exit /b 1
)

echo.
echo [2/3] full install (scaffold + independent venv + pip + model download)...
echo       offline / scaffold only:
echo       "%PY%" -u "%INSTALL_PY%" --yes --scaffold-only --root "%EMB_ROOT%"
echo       manual/offline model mode:
echo       "%PY%" -u "%INSTALL_PY%" --yes --skip-model-download --root "%EMB_ROOT%"
echo       (pip/torch/model may take a long time; progress prints below)
"%PY%" -u "%INSTALL_PY%" --yes --root "%EMB_ROOT%"
set "INSTALL_RC=%ERRORLEVEL%"

echo.
echo [3/3] verify complete model files and mark installed...
"%PY%" -u "%INSTALL_PY%" --yes --mark-only --root "%EMB_ROOT%"
set "MARK_RC=%ERRORLEVEL%"
set "RC=%INSTALL_RC%"
if "%RC%"=="0" if not "%MARK_RC%"=="0" set "RC=%MARK_RC%"

echo.
echo ------------------------------------------------------------
if not "%RC%"=="0" (
  echo addon is not ready; exit code: %RC%
  echo run this script again to resume the automatic download.
  echo if Microsoft VC++ setup was declined, rerun and accept the UAC prompt.
  echo optional mirror override:
  echo   set G4W_HF_ENDPOINT=https://your-mirror.example
  echo optional Torch controls:
  echo   set G4W_TORCH_MODE=gpu
  echo   set G4W_TORCH_INDEX_URL=https://your-pytorch-wheel-index.example/cu128
  echo manual/offline model path:
  echo   %EMB_ROOT%\models\Qwen3-Embedding-0.6B\
  echo after manual placement:
  echo   "%PY%" -u "%INSTALL_PY%" --yes --mark-only --root "%EMB_ROOT%"
) else (
  echo install flow finished and addon layout is ready.
)
echo enable gate:  WeChat /vector on
echo status:       WeChat /vector status
echo disable+stop: WeChat /vector off
echo old .env LM menu is no longer provided by this bat.
echo ------------------------------------------------------------
pause
exit /b %RC%
