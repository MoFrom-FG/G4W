@echo off
rem 把 G4W-WSL 守护加入 Windows 开机启动（当前用户，无需管理员）
rem   安装：install-autostart.cmd [WSL里的包路径] [发行版]
rem   移除：install-autostart.cmd --remove
chcp 65001 >nul
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\G4W-WSL.cmd"

if /i "%~1"=="--remove" (
  if exist "%TARGET%" del /q "%TARGET%" && echo [G4W] 已移除开机自启：%TARGET%
  exit /b 0
)

set "SRC=%~dp0g4w-keepalive.cmd"
if not exist "%SRC%" (echo [G4W] 找不到 %SRC% & pause & exit /b 1)
set "PKGROOT=%~1"
if "%PKGROOT%"=="" set "PKGROOT=%G4W_WSL_ROOT%"
if "%PKGROOT%"=="" set "PKGROOT=~/g4w-linux"
set "DISTRO=%~2"
if "%DISTRO%"=="" set "DISTRO=Ubuntu"

> "%TARGET%" echo @echo off
>>"%TARGET%" echo rem G4W WSL 守护（开机自启）
>>"%TARGET%" echo "%SRC%" "%PKGROOT%" "%DISTRO%"
echo [G4W] 已写入开机自启：%TARGET%
echo [G4W] 包路径=%PKGROOT%  发行版=%DISTRO%
echo [G4W] 下次登录 Windows 时自动保持 WSL 与看板在线。
pause
exit /b 0
