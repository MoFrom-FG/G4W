@echo off
chcp 65001 >nul
if "%G4W_WSL_DISTRO%"=="" (set "DISTRO=Ubuntu") else (set "DISTRO=%G4W_WSL_DISTRO%")
set "PF=%TEMP%\g4w-port.txt"
wsl.exe -d %DISTRO% -e bash -lc "cd ~/g4w-linux && bash tools/linux/status.sh"
wsl.exe -d %DISTRO% -e bash -lc "cat ~/g4w-linux/runtime/dashboard-port 2>/dev/null || echo 18180" > "%PF%" 2>nul
set "PORT="
set /p PORT=<"%PF%"
if "%PORT%"=="" set "PORT=18180"
echo.
echo [G4W] Windows 侧探测：
curl -s -o nul -w "  http://127.0.0.1:%PORT%/ -> %%{http_code}\n" --max-time 4 http://127.0.0.1:%PORT%/
pause
