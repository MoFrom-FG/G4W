[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BasePackage,

    [string]$OutputDirectory,

    [string]$WheelsDirectory,

    [string]$Version = "dev",

    [switch]$CreateZip
)

$ErrorActionPreference = "Stop"

function Get-FullPath([string]$Path) {
    return [IO.Path]::GetFullPath($Path)
}

function Assert-ChildPath([string]$Parent, [string]$Child) {
    $parentFull = Get-FullPath $Parent
    $childFull = Get-FullPath $Child
    if (-not $childFull.StartsWith($parentFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path outside allowed directory: $childFull"
    }
}

function Copy-DirectoryContents([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Missing source directory: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
}

$repoRoot = Get-FullPath (Join-Path $PSScriptRoot "..")
$distRoot = Get-FullPath (Join-Path $repoRoot "dist")
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $distRoot "G4W"
}
$output = Get-FullPath $OutputDirectory
Assert-ChildPath $distRoot $output

$base = Get-FullPath $BasePackage
if (-not (Test-Path -LiteralPath $base)) {
    throw "Base package not found: $base"
}

New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
if (Test-Path -LiteralPath $output) {
    Remove-Item -LiteralPath $output -Recurse -Force
}
New-Item -ItemType Directory -Path $output | Out-Null

$extractRoot = $null
$baseRoot = $base
try {
    if (Test-Path -LiteralPath $base -PathType Leaf) {
        if ([IO.Path]::GetExtension($base) -ne ".zip") {
            throw "BasePackage must be a directory or .zip file."
        }
        $extractRoot = Join-Path $distRoot (".base-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $extractRoot | Out-Null
        Expand-Archive -LiteralPath $base -DestinationPath $extractRoot -Force
        $baseRoot = $extractRoot

        if (-not (Test-Path -LiteralPath (Join-Path $baseRoot "runtime"))) {
            $children = @(Get-ChildItem -LiteralPath $baseRoot -Directory -Force)
            if ($children.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $children[0].FullName "runtime"))) {
                $baseRoot = $children[0].FullName
            }
        }
    }

    $required = @(
        "GenericAgent.exe",
        "runtime\python\python.exe",
        "runtime\wheels",
        "runtime\app"
    )
    foreach ($relative in $required) {
        if (-not (Test-Path -LiteralPath (Join-Path $baseRoot $relative))) {
            throw "Base package is missing required path: $relative"
        }
    }

    Copy-DirectoryContents $baseRoot $output

    if ($WheelsDirectory) {
        $wheelSource = Get-FullPath $WheelsDirectory
        if (-not (Test-Path -LiteralPath $wheelSource -PathType Container)) {
            throw "WheelsDirectory not found: $wheelSource"
        }
        $wheelTarget = Join-Path $output "runtime\wheels"
        if (Test-Path -LiteralPath $wheelTarget) {
            Remove-Item -LiteralPath $wheelTarget -Recurse -Force
        }
        Copy-DirectoryContents $wheelSource $wheelTarget
    }

    $wheelLockPath = Join-Path $PSScriptRoot "wheels.lock.json"
    if (Test-Path -LiteralPath $wheelLockPath) {
        $wheelLock = Get-Content -LiteralPath $wheelLockPath -Raw | ConvertFrom-Json
        $wheelTarget = Join-Path $output "runtime\wheels"
        foreach ($entry in $wheelLock.wheels) {
            $wheelPath = Join-Path $wheelTarget $entry.name
            if (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) {
                throw "Required offline wheel is missing: $($entry.name). Supply -WheelsDirectory with the complete G4W wheel cache."
            }
            $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheelPath).Hash
            if ($actualHash -ne $entry.sha256) {
                throw "Offline wheel hash mismatch: $($entry.name)"
            }
        }
    }

    $rootFiles = @(
        "1_prepare_G4W_ga.bat",
        "2_key_for_ga.bat",
        "3_env_for_G4W.bat",
        "4_login_G4W_ga.bat",
        "5_embedding_for_G4W.bat",
        "browse_short_path_mirror.bat",
        "G4W使用说明.md",
        "LICENSE",
        "readme_GA_desktop.txt",
        "README.md",
        "start_G4W_ga.bat",
        "stop_G4W_ga.bat",
        "uninstall.bat"
    )
    foreach ($relative in $rootFiles) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $relative) -Destination (Join-Path $output $relative) -Force
    }

    Copy-DirectoryContents (Join-Path $repoRoot "images") (Join-Path $output "images")
    Copy-DirectoryContents (Join-Path $repoRoot "tools") (Join-Path $output "tools")
    Copy-DirectoryContents (Join-Path $repoRoot "runtime\app") (Join-Path $output "runtime\app")
    Copy-DirectoryContents (Join-Path $repoRoot "runtime\G4W-main\G4W") (Join-Path $output "runtime\G4W-main\G4W")
    Copy-Item -LiteralPath (Join-Path $repoRoot "runtime\G4W-main\.env.example") -Destination (Join-Path $output "runtime\G4W-main\.env.example") -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "runtime\install_windows.ps1") -Destination (Join-Path $output "runtime\install_windows.ps1") -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "runtime\uninstall_windows.ps1") -Destination (Join-Path $output "runtime\uninstall_windows.ps1") -Force

    $removePaths = @(
        "runtime\app\mykey.py",
        "runtime\app\.venv",
        "runtime\app\temp",
        "runtime\app\memory\file_access_stats.json",
        "runtime\G4W-main\.env",
        "runtime\G4W-main\runtime",
        "runtime\G4W-main\.pytest_cache",
        "runtime\G4W-main\G4W\tests",
        "runtime\G4W-main\G4W\memory\sop\global_mem.txt",
        "runtime\G4W-main\G4W\memory\sop\global_mem_insight.txt",
        "runtime\G4W-main\G4W\memory\sop-user",
        "runtime\G4W-main\plan_G4W_kb",
        "runtime\G4W-data",
        "runtime\G4W-vector-index",
        "runtime\G4W-embedding",
        "runtime\temp"
    )
    foreach ($relative in $removePaths) {
        $target = Get-FullPath (Join-Path $output $relative)
        Assert-ChildPath $output $target
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Recurse -Force
        }
    }

    Get-ChildItem -LiteralPath $output -Recurse -Directory -Force -Filter "__pycache__" | Sort-Object FullName -Descending | ForEach-Object {
        Assert-ChildPath $output $_.FullName
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
    }
    Get-ChildItem -LiteralPath $output -Recurse -File -Force -Filter "*.pyc" | Remove-Item -Force

    $manifestPath = Join-Path $output "G4W_RELEASE_MANIFEST.json"
    if (Test-Path -LiteralPath $manifestPath) {
        Remove-Item -LiteralPath $manifestPath -Force
    }
    $payload = @(Get-ChildItem -LiteralPath $output -Recurse -File -Force)
    $manifest = [ordered]@{
        name = "G4W portable release"
        version = $Version
        built_at = (Get-Date).ToString("o")
        base = "GenericAgent Desktop Portable 1.8"
        base_package = (Split-Path -Leaf $base)
        payload_file_count = $payload.Count
        payload_bytes = [long](($payload | Measure-Object Length -Sum).Sum)
        source_wheel_lock = "packaging/wheels.lock.json"
        excluded_runtime_state = $removePaths
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

    Write-Host "G4W portable directory: $output"

    if ($CreateZip) {
        $zipPath = Get-FullPath (Join-Path $distRoot ("G4W-{0}-win-x64.zip" -f $Version))
        Assert-ChildPath $distRoot $zipPath
        if (Test-Path -LiteralPath $zipPath) {
            Remove-Item -LiteralPath $zipPath -Force
        }
        Compress-Archive -Path (Join-Path $output "*") -DestinationPath $zipPath -CompressionLevel Optimal
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
        $hashPath = "$zipPath.sha256"
        "$hash  $([IO.Path]::GetFileName($zipPath))" | Set-Content -LiteralPath $hashPath -Encoding ASCII
        Write-Host "Release ZIP: $zipPath"
        Write-Host "SHA256: $hash"
    }
}
finally {
    if ($extractRoot -and (Test-Path -LiteralPath $extractRoot)) {
        Assert-ChildPath $distRoot $extractRoot
        Remove-Item -LiteralPath $extractRoot -Recurse -Force
    }
}
