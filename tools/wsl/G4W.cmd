@echo off
rem G4W 一键启动（WSL）：双击即进入浏览器初始化向导
rem   用法：G4W.cmd [WSL里的包路径] [发行版]
chcp 65001 >nul
setlocal
set "PKGROOT=%~1"
if "%PKGROOT%"=="" set "PKGROOT=%G4W_WSL_ROOT%"
if "%PKGROOT%"=="" set "PKGROOT=~/G4W-Linux"
set "DISTRO=%~2"
if "%DISTRO%"=="" set "DISTRO=Ubuntu"
set "PF=%TEMP%\g4w-port.txt"

echo [G4W] 发行版=%DISTRO%  包路径=%PKGROOT%
start "G4W" /min wsl.exe -d %DISTRO% -e bash -lc "cd %PKGROOT% && exec ./G4W"

wsl.exe -d %DISTRO% -e bash -lc "cat %PKGROOT%/runtime/dashboard-port 2>/dev/null || echo 18180" > "%PF%" 2>nul
set "PORT="
set /p PORT=<"%PF%"
if "%PORT%"=="" set "PORT=18180"
set "URL=http://127.0.0.1:%PORT%/"
echo [G4W] 地址：%URL%
set /a N=0
:wait
timeout /t 1 >nul
curl -s -o nul --max-time 2 %URL% && goto ready
set /a N+=1
if %N% LSS 45 goto wait
echo [G4W] 未就绪：请在 WSL 里执行 bash %PKGROOT%/tools/linux/status.sh 查看
pause
exit /b 1
:ready
start "" %URL%
exit /b 0
