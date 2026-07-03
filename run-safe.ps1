$ErrorActionPreference = "Stop"

if (Test-Path ".\.venv\Scripts\python.exe") {
    .\.venv\Scripts\python.exe -m photo_dedupe --safe-mode
} else {
    py -m photo_dedupe --safe-mode
}
