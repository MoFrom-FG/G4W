@echo off
rem G4W Linux 便携包（WSL）一键启动看板
rem   用法：dashboard.cmd [WSL里的包路径] [发行版]
rem   例：  dashboard.cmd ~/G4W-Linux Ubuntu
chcp 65001 >nul
setlocal
set "ROOT=%~1"
if "%ROOT%"=="" set "ROOT=~/G4W-Linux"
if "%G4W_WSL_ROOT%"=="" (set "PKGROOT=%ROOT%") else (set "PKGROOT=%G4W_WSL_ROOT%")
set "DISTRO=%~2"
if "%DISTRO%"=="" set "DISTRO=Ubuntu"
set "PF=%TEMP%\g4w-port.txt"

echo [G4W] 发行版=%DISTRO%  包路径=%PKGROOT%
echo [G4W] 唤醒 WSL 并保持常驻（会出现一个最小化窗口，请勿关闭）...
start "G4W-WSL-Keepalive" /min wsl.exe -d %DISTRO% -e bash -lc "cd %PKGROOT% && exec bash tools/linux/keepalive.sh"

wsl.exe -d %DISTRO% -e bash -lc "cat %PKGROOT%/runtime/dashboard-port 2>/dev/null || echo 18180" > "%PF%" 2>nul
set "PORT="
set /p PORT=<"%PF%"
if "%PORT%"=="" set "PORT=18180"
set "URL=http://127.0.0.1:%PORT%/"
echo [G4W] 看板地址：%URL%
set /a N=0
:wait
timeout /t 1 >nul
curl -s -o nul --max-time 2 %URL% && goto ready
set /a N+=1
if %N% LSS 40 goto wait
echo [G4W] 未就绪：请在 WSL 里执行 bash %PKGROOT%/tools/linux/status.sh 查看原因
pause
exit /b 1
:ready
echo [G4W] 就绪，正在打开浏览器
start "" %URL%
exit /b 0
