param([switch]$SkipInstall)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-CheckedNative {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE."
    }
}

function Assert-WorkspacePath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $workspacePrefix = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Build target is outside the workspace: $resolved"
    }
    return $resolved
}

Push-Location $PSScriptRoot
$previousBuildInfo = $env:PHOTO_MANAGE_BUILD_INFO
try {
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Invoke-CheckedNative "py" @("-3.10", "-m", "venv", ".venv")
    }
    if (-not $SkipInstall) {
        Invoke-CheckedNative $python @("-m", "pip", "install", "-r", "requirements.txt")
        Invoke-CheckedNative $python @("-m", "pip", "install", "pyinstaller")
    }

    $commitOutput = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the Git commit for this build." }
    $commit = ($commitOutput | Out-String).Trim()
    $dirtyOutput = & git diff --name-only HEAD
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect the Git worktree." }
    if ($dirtyOutput) { throw "Commit tracked changes before building a traceable release." }
    $untrackedSource = & git ls-files --others --exclude-standard -- photo_dedupe
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect untracked source files." }
    if ($untrackedSource) { throw "Commit new application source files before building a traceable release." }

    $runId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $staging = Assert-WorkspacePath (Join-Path $PSScriptRoot "build\staging\$runId")
    New-Item -ItemType Directory -Path $staging | Out-Null
    $metadataPath = Join-Path $staging "build-info.json"
    Invoke-CheckedNative $python @("-m", "photo_dedupe.core.build_info", "--write", $metadataPath, "--commit", $commit)
    $env:PHOTO_MANAGE_BUILD_INFO = $metadataPath
    $stagingDist = Join-Path $staging "dist"
    Invoke-CheckedNative $python @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $stagingDist, "--workpath", (Join-Path $staging "work"), "PhotoManageGPT.spec")

    $candidate = Assert-WorkspacePath (Join-Path $stagingDist "PhotoManageGPT")
    Copy-Item -LiteralPath $metadataPath -Destination (Join-Path $candidate "build-info.json")
    Invoke-CheckedNative $python @("-m", "photo_dedupe.core.release_check", "--release-check", "--build-only", "--dist-dir", $candidate, "--require-build-info")
    $smoke = Start-Process -FilePath (Join-Path $candidate "PhotoManageGPT.exe") -ArgumentList "--startup-smoke-test" -WindowStyle Hidden -Wait -PassThru
    if ($smoke.ExitCode -ne 0) { throw "Packaged startup smoke test failed with exit code $($smoke.ExitCode). Existing build was retained." }

    $destination = Assert-WorkspacePath (Join-Path $PSScriptRoot "dist\PhotoManageGPT")
    $backup = Assert-WorkspacePath (Join-Path $PSScriptRoot "dist\backups\PhotoManageGPT-$runId")
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    if (Test-Path -LiteralPath $destination) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
        Move-Item -LiteralPath $destination -Destination $backup
    }
    try {
        Move-Item -LiteralPath $candidate -Destination $destination
    } catch {
        if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $destination)) {
            Move-Item -LiteralPath $backup -Destination $destination
        }
        throw
    }
    Write-Host "Build verified: $destination\PhotoManageGPT.exe"
    if (Test-Path -LiteralPath $backup) { Write-Host "Previous build retained: $backup" }
} finally {
    if ($null -eq $previousBuildInfo) {
        Remove-Item Env:\PHOTO_MANAGE_BUILD_INFO -ErrorAction SilentlyContinue
    } else {
        $env:PHOTO_MANAGE_BUILD_INFO = $previousBuildInfo
    }
    Pop-Location
}
