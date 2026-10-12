@echo off
rem G4W WSL 守护（供开机自启调用）：保持发行版运行 + 看板自愈
chcp 65001 >nul
setlocal
set "PKGROOT=%~1"
if "%PKGROOT%"=="" set "PKGROOT=%G4W_WSL_ROOT%"
if "%PKGROOT%"=="" set "PKGROOT=~/g4w-linux"
set "DISTRO=%~2"
if "%DISTRO%"=="" set "DISTRO=Ubuntu"
wsl.exe -d %DISTRO% -e bash -lc "cd %PKGROOT% && exec bash tools/linux/keepalive.sh" >nul 2>&1
