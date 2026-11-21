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

function Invoke-IsolatedPyInstaller {
    param([string]$Python, [string[]]$Arguments)
    $basePrefixOutput = & $Python -c "import sys; print(sys.base_prefix)"
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the base Python runtime for packaging." }
    $basePrefix = ($basePrefixOutput | Out-String).Trim()
    if (-not (Test-Path -LiteralPath $basePrefix -PathType Container)) {
        throw "The base Python runtime directory does not exist: $basePrefix"
    }
    # PyInstaller searches PATH while resolving DLLs. Inheriting unrelated tools
    # can select an incompatible ICU/Qt DLL before the Windows system copy.
    $isolatedEntries = @(
        (Split-Path -Parent ([System.IO.Path]::GetFullPath($Python))),
        $basePrefix,
        (Join-Path $env:SystemRoot "System32"),
        $env:SystemRoot
    )
    $previousPath = $env:PATH
    try {
        $env:PATH = ($isolatedEntries | Select-Object -Unique) -join [System.IO.Path]::PathSeparator
        Invoke-CheckedNative $Python $Arguments
    } finally {
        if ($null -eq $previousPath) {
            Remove-Item Env:\PATH -ErrorAction SilentlyContinue
        } else {
            $env:PATH = $previousPath
        }
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

function Assert-BuildSourceState {
    param([string]$ExpectedCommit)
    $headOutput = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the current Git commit." }
    if ((($headOutput | Out-String).Trim()) -ne $ExpectedCommit) {
        throw "Git HEAD changed during the build. Existing build was retained; rebuild from the new commit."
    }
    $dirtyOutput = & git diff --name-only HEAD
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect the Git worktree." }
    if ($dirtyOutput) { throw "Tracked source changed. Commit changes and rebuild before promotion." }
    $untrackedSource = & git ls-files --others --exclude-standard -- photo_dedupe
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect untracked source files." }
    if ($untrackedSource) { throw "New application source files are uncommitted. Commit changes and rebuild before promotion." }
}

function Invoke-BuildSmokeTest {
    param(
        [string]$Executable, [string]$WorkingDirectory, [string]$LogDirectory,
        [string[]]$Arguments = @("--startup-smoke-test"), [int]$TimeoutMilliseconds = 60000
    )
    $stdout = Join-Path $LogDirectory "smoke.stdout.log"
    $stderr = Join-Path $LogDirectory "smoke.stderr.log"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            # Kill only the process started for this smoke test, not user app instances.
            $process.Kill()
            $null = $process.WaitForExit(5000)
            throw "Startup smoke test timed out after $TimeoutMilliseconds ms. Logs: $stdout ; $stderr . Existing build was retained."
        }
        if ($process.ExitCode -ne 0) {
            throw "Startup smoke test failed with exit code $($process.ExitCode). Logs: $stdout ; $stderr . Existing build was retained."
        }
    } finally {
        $process.Dispose()
    }
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
    Assert-BuildSourceState $commit

    $runId = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $staging = Assert-WorkspacePath (Join-Path $PSScriptRoot "build\staging\$runId")
    New-Item -ItemType Directory -Path $staging | Out-Null
    $metadataPath = Join-Path $staging "build-info.json"
    Invoke-CheckedNative $python @("-m", "photo_dedupe.core.build_info", "--write", $metadataPath, "--commit", $commit)
    $env:PHOTO_MANAGE_BUILD_INFO = $metadataPath
    $stagingDist = Join-Path $staging "dist"
    Invoke-IsolatedPyInstaller $python @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $stagingDist, "--workpath", (Join-Path $staging "work"), "PhotoManageGPT.spec")

    $candidate = Assert-WorkspacePath (Join-Path $stagingDist "PhotoManageGPT")
    Copy-Item -LiteralPath $metadataPath -Destination (Join-Path $candidate "build-info.json")
    Invoke-CheckedNative $python @("-m", "photo_dedupe.core.release_check", "--release-check", "--build-only", "--dist-dir", $candidate, "--require-build-info")
    Invoke-BuildSmokeTest -Executable (Join-Path $candidate "PhotoManageGPT.exe") -WorkingDirectory $candidate -LogDirectory $staging

    Assert-BuildSourceState $commit
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
