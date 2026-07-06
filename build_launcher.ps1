$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    .\install.ps1
}

.\.venv\Scripts\python.exe -m pip install pyinstaller

if (Test-Path -LiteralPath ".\dist\SRTMatcherSetup.exe") {
    Remove-Item -LiteralPath ".\dist\SRTMatcherSetup.exe" -Force
}
if (Test-Path -LiteralPath ".\dist\nsis_payload") {
    Remove-Item -LiteralPath ".\dist\nsis_payload" -Recurse -Force
}

.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --distpath ".\dist\nsis_payload" `
    --workpath ".\build\SRTMatcherPayload" `
    --name "SRTMatcher" `
    --add-data "app.py;." `
    --add-data "qt_app.py;." `
    --add-data "requirements.txt;." `
    --add-data "README.md;." `
    .\bootstrap.py

$makensisCandidates = @(
    "C:\Program Files (x86)\NSIS\makensis.exe",
    "C:\Program Files\NSIS\makensis.exe"
)
$makensis = $makensisCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $makensis) {
    $cmd = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($cmd) { $makensis = $cmd.Source }
}
if (-not $makensis) {
    throw "NSIS makensis.exe not found. Install NSIS first."
}

$nsiPath = Join-Path $PSScriptRoot "installer.nsi"
$nsiText = [System.IO.File]::ReadAllText($nsiPath, [System.Text.Encoding]::UTF8)
$utf8Bom = [System.Text.UTF8Encoding]::new($true)
[System.IO.File]::WriteAllText($nsiPath, $nsiText, $utf8Bom)

& $makensis ".\installer.nsi"

if (Test-Path -LiteralPath ".\dist\nsis_payload") {
    Remove-Item -LiteralPath ".\dist\nsis_payload" -Recurse -Force
}

Write-Host ""
Write-Host "NSIS installer build complete: .\dist\SRTMatcherSetup.exe"
