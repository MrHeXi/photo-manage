$ErrorActionPreference = "Stop"

.\build.ps1

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "py"
}

$version = (& $python -c "import photo_dedupe; print(photo_dedupe.__version__)").Trim()
$releaseDir = Join-Path "release" "PhotoManageGPT-v$version-win64"
$zipPath = "$releaseDir.zip"

if (Test-Path $releaseDir) {
    Remove-Item -LiteralPath $releaseDir -Recurse -Force
}
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

New-Item -ItemType Directory -Path $releaseDir | Out-Null
Copy-Item -Path "dist\PhotoManageGPT\*" -Destination $releaseDir -Recurse
Copy-Item -Path "README.md" -Destination (Join-Path $releaseDir "README.md")

$safeModeLauncher = @"
@echo off
start "" "%~dp0PhotoManageGPT.exe" --safe-mode
"@
Set-Content -Path (Join-Path $releaseDir "PhotoManageGPT-safe-mode.cmd") -Value $safeModeLauncher -Encoding ASCII

Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -Force
Write-Host "Release package: $zipPath"
