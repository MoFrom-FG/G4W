@echo off
chcp 65001 >nul
if "%G4W_WSL_DISTRO%"=="" (set "DISTRO=Ubuntu") else (set "DISTRO=%G4W_WSL_DISTRO%")
echo [G4W] 停止看板服务与主服务...
wsl.exe -d %DISTRO% -e bash -lc "systemctl --user stop g4w-dashboard.service g4w-main.service 2>/dev/null; bash ~/g4w-linux/tools/linux/status.sh | head -6"
echo [G4W] 说明：最小化的 keepalive 窗口可手动关闭；关闭后 WSL 会在空闲时自动停机。
pause
