$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$exePath = Join-Path $scriptDir "PhotoManageGPT.exe"

if (-not (Test-Path -LiteralPath $exePath)) {
    throw "PhotoManageGPT.exe was not found next to this script. Please run this script from the extracted release folder."
}

$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktop)) {
    throw "Windows desktop folder was not found."
}

function New-PhotoManageShortcut {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $false)][string]$Arguments = "",
        [Parameter(Mandatory = $true)][string]$Description
    )

    $shortcutPath = Join-Path $desktop "$Name.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $exePath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $scriptDir
    $shortcut.IconLocation = "$exePath,0"
    $shortcut.Description = $Description
    $shortcut.Save()
    Write-Host "Created shortcut: $shortcutPath"
}

New-PhotoManageShortcut -Name "Photo Manage GPT" -Description "Photo Manage GPT"
New-PhotoManageShortcut -Name "Photo Manage GPT Safe Mode" -Arguments "--safe-mode" -Description "Photo Manage GPT safe mode"

Write-Host "Done. You can now start Photo Manage GPT from the desktop."
