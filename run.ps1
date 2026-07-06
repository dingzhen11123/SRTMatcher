$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    Write-Host ".venv not found. Running installer first..."
    .\install.ps1
}

.\.venv\Scripts\python.exe .\qt_app.py
