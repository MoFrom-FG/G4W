@echo off
rem ============================================================
rem  构建 G4W 控制中心桌面壳（PyInstaller + pywebview + pystray）
rem  用 GA venv 作为构建环境；产物：dist\G4W.exe（放 G4W 根目录使用）
rem ============================================================
setlocal
set "VENV=%~dp0..\runtime\app\.venv\Scripts"
if not exist "%VENV%\python.exe" (
  echo [G4W Desktop] 未找到 GA venv，请先运行 1_prepare_G4W_ga.bat
  pause
  exit /b 1
)
"%VENV%\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name G4W --icon "%~dp0icon.ico" ^
  --add-data "%~dp0icon.ico;." ^
  --add-data "%~dp0loading.html;." ^
  --add-data "%~dp0wizard.html;." ^
  --add-data "%~dp0wizard_prepare.py;." ^
  --add-data "%~dp0..\runtime\app\.venv\Lib\site-packages\webview\js;webview\js" ^
  --collect-all clr ^
  "%~dp0shell.py"
if errorlevel 1 (
  echo 构建失败，查看上方错误信息
  pause
  exit /b 1
)
echo.
echo 构建完成：dist\G4W.exe
echo 复制到 G4W 根目录（与 runtime 平级）后双击即可使用。
pause
