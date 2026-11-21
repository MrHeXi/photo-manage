param([switch]$SkipInstall, [switch]$SkipBuild)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-PackageNative {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE." }
}

function Assert-PackagePath {
    param([string]$Path)
    $resolved = [System.IO.Path]::GetFullPath($Path)
    $workspacePrefix = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($workspacePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Package target is outside the workspace: $resolved"
    }
    return $resolved
}

function Assert-PackageSourceState {
    param([string]$ExpectedCommit)
    $headOutput = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the current Git commit." }
    if ((($headOutput | Out-String).Trim()) -ne $ExpectedCommit) {
        throw "Git HEAD does not match the build identity. Rebuild before publishing."
    }
    $dirtyOutput = & git diff --name-only HEAD
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect the Git worktree." }
    if ($dirtyOutput) { throw "Tracked source changed. Commit changes and rebuild before publishing." }
    $untrackedSource = & git ls-files --others --exclude-standard -- photo_dedupe
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect untracked source files." }
    if ($untrackedSource) { throw "New application source files are uncommitted. Commit changes and rebuild before publishing." }
}

function Invoke-PackageSmokeTest {
    param(
        [string]$Executable, [string]$WorkingDirectory, [string]$LogDirectory,
        [string[]]$Arguments = @("--startup-smoke-test"), [int]$TimeoutMilliseconds = 60000
    )
    $stdout = Join-Path $LogDirectory "smoke.stdout.log"
    $stderr = Join-Path $LogDirectory "smoke.stderr.log"
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $process.Kill()
            $null = $process.WaitForExit(5000)
            throw "Startup smoke test timed out after $TimeoutMilliseconds ms. Logs: $stdout ; $stderr . Previous packages were retained."
        }
        if ($process.ExitCode -ne 0) {
            throw "Startup smoke test failed with exit code $($process.ExitCode). Logs: $stdout ; $stderr . Previous packages were retained."
        }
    } finally {
        $process.Dispose()
    }
}

Push-Location $PSScriptRoot
try {
    if (-not $SkipBuild) { & (Join-Path $PSScriptRoot "build.ps1") -SkipInstall:$SkipInstall }
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    $distDir = Join-Path $PSScriptRoot "dist\PhotoManageGPT"
    $metadata = Get-Content -LiteralPath (Join-Path $distDir "build-info.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-PackageSourceState $metadata.commit
    $packageName = "PhotoManageGPT-$($metadata.build_id)-win64"
    if ($packageName -notmatch '^[A-Za-z0-9_.-]+$') { throw "Invalid build identity for a package filename." }
    $releaseDir = Assert-PackagePath (Join-Path $PSScriptRoot "release\$packageName")
    $zipPath = "$releaseDir.zip"
    if ((Test-Path -LiteralPath $releaseDir) -or (Test-Path -LiteralPath $zipPath) -or (Test-Path -LiteralPath "$zipPath.sha256")) {
        throw "This build already has a package. Existing package was retained; rebuild to create a new identity."
    }
    $stageRoot = Assert-PackagePath (Join-Path $PSScriptRoot ("release\.staging\" + [Guid]::NewGuid().ToString("N")))
    $stageDir = Assert-PackagePath (Join-Path $stageRoot $packageName)
    New-Item -ItemType Directory -Path $stageDir | Out-Null
    Copy-Item -Path (Join-Path $distDir "*") -Destination $stageDir -Recurse
    foreach ($document in @("README.md", "CHANGELOG.md", "QUICKSTART.md", "install-shortcut.ps1", "remove-shortcut.ps1")) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $document) -Destination (Join-Path $stageDir $document)
    }
    $safeModeLauncher = "@echo off`r`nstart `"`" `"%~dp0PhotoManageGPT.exe`" --safe-mode`r`n"
    Set-Content -LiteralPath (Join-Path $stageDir "PhotoManageGPT-safe-mode.cmd") -Value $safeModeLauncher -Encoding ASCII

    Invoke-PackageSmokeTest -Executable (Join-Path $stageDir "PhotoManageGPT.exe") -WorkingDirectory $stageDir -LogDirectory $stageRoot
    Invoke-PackageNative $python @("-m", "photo_dedupe", "--write-release-manifest", $stageDir)
    Invoke-PackageNative $python @("-m", "photo_dedupe.core.release_check", "--release-check", "--release-dir", $stageDir, "--require-build-info")

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $stageZip = Join-Path $stageRoot "$packageName.zip"
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $stageDir, $stageZip, [System.IO.Compression.CompressionLevel]::Optimal, $false
    )
    $zipHash = (Get-FileHash -LiteralPath $stageZip -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath "$stageZip.sha256" -Value "$zipHash  $packageName.zip" -Encoding ASCII
    Invoke-PackageNative $python @("-m", "photo_dedupe.core.release_check", "--release-check", "--release-dir", $stageDir, "--zip-path", $stageZip, "--require-build-info")

    # Promotion occurs only after the directory, startup, archive and checksum pass.
    Assert-PackageSourceState $metadata.commit
    try {
        Move-Item -LiteralPath $stageDir -Destination $releaseDir
        Move-Item -LiteralPath $stageZip -Destination $zipPath
        Move-Item -LiteralPath "$stageZip.sha256" -Destination "$zipPath.sha256"
    } catch {
        # Return this attempt's files to staging; earlier published versions are untouched.
        if (Test-Path -LiteralPath $releaseDir) { Move-Item -LiteralPath $releaseDir -Destination $stageDir }
        if (Test-Path -LiteralPath $zipPath) { Move-Item -LiteralPath $zipPath -Destination $stageZip }
        if (Test-Path -LiteralPath "$zipPath.sha256") { Move-Item -LiteralPath "$zipPath.sha256" -Destination "$stageZip.sha256" }
        throw
    }
    Write-Host "Verified release: $releaseDir\PhotoManageGPT.exe"
    Write-Host "Release package: $zipPath"
    Write-Host "SHA-256: $zipHash"
} finally {
    Pop-Location
}
