Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspace = Split-Path -Parent $MyInvocation.MyCommand.Path
$specPath = Join-Path $workspace 'WaveformMonitor_OneFile.spec'
if (-not (Test-Path -LiteralPath $specPath)) {
    throw "Build spec file not found: $specPath"
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$tempRoot = Join-Path $env:TEMP "WaveformMonitorBuild_$timestamp"
$tempDist = Join-Path $tempRoot 'dist'
$tempBuild = Join-Path $tempRoot 'build'
$finalDist = Join-Path $workspace 'dist'
$finalExe = Join-Path $finalDist "WaveformMonitor_OneFile_$timestamp.exe"

New-Item -ItemType Directory -Force -Path $tempDist | Out-Null
New-Item -ItemType Directory -Force -Path $tempBuild | Out-Null
New-Item -ItemType Directory -Force -Path $finalDist | Out-Null

Push-Location $workspace
try {
    python -m PyInstaller --noconfirm --distpath $tempDist --workpath $tempBuild $specPath
    $builtExe = Join-Path $tempDist 'WaveformMonitor_OneFile.exe'
    if (-not (Test-Path -LiteralPath $builtExe)) {
        throw "EXE was not found after build: $builtExe"
    }
    Copy-Item -LiteralPath $builtExe -Destination $finalExe -Force
    Write-Output $finalExe
}
finally {
    Pop-Location
}
