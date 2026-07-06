$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "py"
}

$version = (& $python -c "import photo_dedupe; print(photo_dedupe.__version__)").Trim()
$releaseDir = Join-Path "release" "PhotoManageGPT-v$version-win64"
$zipPath = "$releaseDir.zip"

function Remove-PathWithRetry {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 5) {
                throw "无法清理旧发布产物 $Path，可能正在被资源管理器、杀毒软件或压缩工具占用。请关闭相关窗口后重试。原始错误：$($_.Exception.Message)"
            }
            Start-Sleep -Milliseconds 800
        }
    }
}

Remove-PathWithRetry $releaseDir
Remove-PathWithRetry $zipPath
Remove-PathWithRetry "$zipPath.sha256"

.\build.ps1

New-Item -ItemType Directory -Path $releaseDir | Out-Null
Copy-Item -Path "dist\PhotoManageGPT\*" -Destination $releaseDir -Recurse
Copy-Item -Path "README.md" -Destination (Join-Path $releaseDir "README.md")
Copy-Item -Path "CHANGELOG.md" -Destination (Join-Path $releaseDir "CHANGELOG.md")

$safeModeLauncher = @"
@echo off
start "" "%~dp0PhotoManageGPT.exe" --safe-mode
"@
Set-Content -Path (Join-Path $releaseDir "PhotoManageGPT-safe-mode.cmd") -Value $safeModeLauncher -Encoding ASCII

$smokeExe = Join-Path $releaseDir "PhotoManageGPT.exe"
& $smokeExe --startup-smoke-test

& $python -m photo_dedupe --write-release-manifest $releaseDir

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zipFullPath = Join-Path (Resolve-Path ".") $zipPath
$releaseFullPath = (Resolve-Path $releaseDir).Path
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $releaseFullPath,
    $zipFullPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $false
)

$zipHash = (Get-FileHash -Path $zipFullPath -Algorithm SHA256).Hash.ToLowerInvariant()
$zipFileName = Split-Path -Leaf $zipPath
Set-Content -Path "$zipPath.sha256" -Value "$zipHash  $zipFileName" -Encoding ASCII

& $python -m photo_dedupe --release-check
Write-Host "Release package: $zipPath"
