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

function Assert-LastExit([string]$step) {
    # $ErrorActionPreference 管不到外部程序，必须显式检查退出码。
    if ($LASTEXITCODE -ne 0) {
        throw "$step failed with exit code $LASTEXITCODE"
    }
}

.\.venv\Scripts\python.exe -m pip install pyinstaller
Assert-LastExit "pip install pyinstaller"

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

$buildTime = Get-Date
# VIProductVersion 需要 4 段数字，每段不超过 65535。
$appVersion = "{0}.{1}.{2}.{3}" -f $buildTime.Year, $buildTime.Month, $buildTime.Day, ($buildTime.Hour * 100 + $buildTime.Minute)

if (Test-Path -LiteralPath ".\dist\nsis_payload") {
    Remove-Item -LiteralPath ".\dist\nsis_payload" -Recurse -Force
}

$assetsDir = ".\build\installer-assets"
.\.venv\Scripts\python.exe .\make_installer_assets.py $assetsDir
Assert-LastExit "make_installer_assets.py"

# 打包清单只维护一份：bootstrap.SOURCE_FILES 同时决定打包、安装复制和升级指纹。
$sourceFiles = @(.\.venv\Scripts\python.exe -c "import bootstrap; print('\n'.join(bootstrap.SOURCE_FILES))")
Assert-LastExit "read bootstrap.SOURCE_FILES"
$addData = @()
foreach ($name in $sourceFiles) {
    $name = $name.Trim()
    if (-not $name) { continue }
    if (-not (Test-Path -LiteralPath $name -PathType Leaf)) {
        throw "SOURCE_FILES entry not found: $name"
    }
    $addData += @("--add-data", "$name;.")
}
$addData += @("--add-data", "$assetsDir\srtmatcher-logo-64.png;.")

.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --icon ".\srtmatcher.ico" `
    --distpath ".\dist\nsis_payload" `
    --workpath ".\build\SRTMatcherPayload" `
    --name "SRTMatcher" `
    @addData `
    .\bootstrap.py
Assert-LastExit "PyInstaller"

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

& $makensis "/DOUTPUT_FILE=$outputRelativePath" "/DAPP_VERSION=$appVersion" "/DBUILD_ID=$buildId" ".\installer.nsi"
Assert-LastExit "makensis"
if (-not (Test-Path -LiteralPath $outputFullPath)) {
    throw "makensis finished but installer not found: $outputFullPath"
}

if (Test-Path -LiteralPath ".\dist\nsis_payload") {
    Remove-Item -LiteralPath ".\dist\nsis_payload" -Recurse -Force
}

Write-Host ""
Write-Host "NSIS installer build complete: $outputFullPath"
