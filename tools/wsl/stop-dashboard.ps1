param([string]$Distro = "Ubuntu")
wsl.exe -d $Distro -e bash -lc "cd ~/g4w-linux && bash tools/linux/stop.sh" | Write-Output
Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | ForEach-Object { Write-Output ("wsl.exe pid=" + $_.ProcessId) }
