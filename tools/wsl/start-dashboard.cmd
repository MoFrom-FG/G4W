@echo off
chcp 65001 >nul
setlocal
if "%G4W_WSL_DISTRO%"=="" (set "DISTRO=Ubuntu") else (set "DISTRO=%G4W_WSL_DISTRO%")
set "LOG=%~dp0..\..\temp\g4w-wsl-dashboard.log"
start "" /min wsl.exe -d %DISTRO% -e bash -lc "cd ~/g4w-linux && bash tools/linux/dashboard.sh --bg && exec sleep infinity" >>"%LOG%" 2>&1
exit /b 0
