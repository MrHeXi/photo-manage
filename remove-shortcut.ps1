$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$desktop = [Environment]::GetFolderPath("Desktop")
if ([string]::IsNullOrWhiteSpace($desktop)) {
    throw "Windows desktop folder was not found."
}

$shortcutNames = @(
    "Photo Manage GPT.lnk",
    "Photo Manage GPT Safe Mode.lnk"
)

foreach ($shortcutName in $shortcutNames) {
    $shortcutPath = Join-Path $desktop $shortcutName
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-Host "Removed shortcut: $shortcutPath"
    } else {
        Write-Host "Shortcut not found: $shortcutPath"
    }
}

Write-Host "Done."
