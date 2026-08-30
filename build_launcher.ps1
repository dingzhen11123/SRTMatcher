$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    .\install.ps1
}

$ffmpegCandidates = @()
if ($env:FFMPEG_EXE) {
    $ffmpegCandidates += $env:FFMPEG_EXE
}
$ffmpegCandidates += @(
    (Join-Path $PSScriptRoot "vendor\ffmpeg.exe"),
    "C:\ffmpeg\ffmpeg.exe"
)
$ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if ($ffmpegCommand) {
    $ffmpegCandidates += $ffmpegCommand.Source
}
$ffmpeg = $ffmpegCandidates | Where-Object {
    $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
} | Select-Object -First 1
if (-not $ffmpeg) {
    throw "ffmpeg.exe not found. Set FFMPEG_EXE or place it at vendor\ffmpeg.exe."
}
Write-Host "Bundling FFmpeg: $ffmpeg"

.\.venv\Scripts\python.exe -m pip install pyinstaller

$buildIdLine = Select-String -LiteralPath ".\bootstrap.py" -Pattern '^APP_BUILD_ID\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $buildIdLine) {
    throw "APP_BUILD_ID not found in bootstrap.py"
}
$buildId = $buildIdLine.Matches[0].Groups[1].Value -replace '[^A-Za-z0-9._-]', '-'
$buildStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputFileName = "SRTMatcherSetup-$buildId-$buildStamp.exe"
$outputRelativePath = "dist\$outputFileName"
$outputFullPath = Join-Path $PSScriptRoot $outputRelativePath
if (Test-Path -LiteralPath $outputFullPath) {
    throw "Refusing to overwrite existing installer: $outputFullPath"
}

if (Test-Path -LiteralPath ".\dist\nsis_payload") {
    Remove-Item -LiteralPath ".\dist\nsis_payload" -Recurse -Force
}

.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --icon ".\srtmatcher.ico" `
    --distpath ".\dist\nsis_payload" `
    --workpath ".\build\SRTMatcherPayload" `
    --name "SRTMatcher" `
    --add-data "app.py;." `
    --add-data "qt_app.py;." `
    --add-data "task_runtime.py;." `
    --add-data "model_runtime.py;." `
    --add-data "ai_transport.py;." `
    --add-data "asr_runtime.py;." `
    --add-data "batch_runtime.py;." `
    --add-data "requirements.txt;." `
    --add-data "README.md;." `
    --add-data "FFMPEG-NOTICE.txt;." `
    --add-data "srtmatcher-logo.png;." `
    --add-data "srtmatcher.ico;." `
    .\bootstrap.py

Copy-Item -LiteralPath $ffmpeg -Destination ".\dist\nsis_payload\ffmpeg.exe" -Force

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

& $makensis "/DOUTPUT_FILE=$outputRelativePath" ".\installer.nsi"

if (Test-Path -LiteralPath ".\dist\nsis_payload") {
    Remove-Item -LiteralPath ".\dist\nsis_payload" -Recurse -Force
}

Write-Host ""
Write-Host "NSIS installer build complete: $outputFullPath"
