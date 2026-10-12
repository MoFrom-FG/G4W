@echo off
chcp 65001 >nul
setlocal
if "%G4W_WSL_DISTRO%"=="" (set "DISTRO=Ubuntu") else (set "DISTRO=%G4W_WSL_DISTRO%")
set "PF=%TEMP%\g4w-port.txt"

echo [G4W] 唤醒 WSL 并保持常驻（会开一个最小化窗口，请勿关闭）...
start "G4W-WSL-Keepalive" /min wsl.exe -d %DISTRO% -e bash -lc "cd ~/g4w-linux && exec bash tools/linux/keepalive.sh"

wsl.exe -d %DISTRO% -e bash -lc "cat ~/g4w-linux/runtime/dashboard-port 2>/dev/null || echo 18180" > "%PF%" 2>nul
set "PORT="
set /p PORT=<"%PF%"
if "%PORT%"=="" set "PORT=18180"
set "URL=http://127.0.0.1:%PORT%/"
echo [G4W] 看板地址：%URL%
echo [G4W] 等待就绪（最多 40 秒）...
set /a N=0
:wait
timeout /t 1 >nul
curl -s -o nul --max-time 2 %URL% && goto ready
set /a N+=1
if %N% LSS 40 goto wait
echo [G4W] 未就绪。请在 WSL 里执行： bash ~/g4w-linux/tools/linux/status.sh
pause
exit /b 1
:ready
echo [G4W] 就绪，正在打开浏览器
start "" %URL%
exit /b 0
