param([string]$Distro = "Ubuntu")
$inner = 'cd ~/g4w-linux && exec bash tools/linux/dashboard.sh >> runtime/g4w-dashboard.log 2>&1'
$existing = Get-Process wsl -ErrorAction SilentlyContinue
Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "-e", "bash", "-lc", $inner) -WindowStyle Hidden
Write-Output "[G4W] 已发出启动指令（wsl.exe 常驻持有会话，看板随 WSL 存活）"
