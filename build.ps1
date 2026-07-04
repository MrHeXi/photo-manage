$ErrorActionPreference = "Stop"

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -3.10 -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean PhotoManageGPT.spec
.\.venv\Scripts\python.exe -m photo_dedupe --release-check

Write-Host "Build finished: dist\PhotoManageGPT\PhotoManageGPT.exe"
