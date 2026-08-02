[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$WheelsDirectory
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$wheels = [IO.Path]::GetFullPath($WheelsDirectory)
if (-not (Test-Path -LiteralPath $wheels -PathType Container)) {
    throw "Wheels directory not found: $wheels"
}

$entries = @(Get-ChildItem -LiteralPath $wheels -File -Filter "*.whl" | Sort-Object Name | ForEach-Object {
    [ordered]@{
        name = $_.Name
        bytes = $_.Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash
    }
})
if (-not $entries.Count) {
    throw "No wheel files found in: $wheels"
}

$lock = [ordered]@{
    format = 1
    generated_at = (Get-Date).ToString("o")
    wheel_count = $entries.Count
    wheels = $entries
}
$lockPath = Join-Path $PSScriptRoot "wheels.lock.json"
$lock | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $lockPath -Encoding UTF8
Write-Host "Wrote $lockPath with $($entries.Count) wheels."
