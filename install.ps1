$ErrorActionPreference = "Stop"

Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0+cu128 torchaudio==2.8.0+cu128 torchvision==0.23.0+cu128

$scipyDistn = ".\.venv\Lib\site-packages\scipy\stats\_distn_infrastructure.py"
if (Test-Path -LiteralPath $scipyDistn) {
    $content = Get-Content -LiteralPath $scipyDistn -Raw
    $content = $content.Replace("del obj", "globals().pop('obj', None)")
    Set-Content -LiteralPath $scipyDistn -Value $content -Encoding UTF8
}

$torchUfuncs = ".\.venv\Lib\site-packages\torch\_numpy\_ufuncs.py"
if (Test-Path -LiteralPath $torchUfuncs) {
    $content = Get-Content -LiteralPath $torchUfuncs -Raw
    $content = $content.Replace("for name in _binary:`r`n    ufunc = getattr(_binary_ufuncs_impl, name)`r`n    vars()[name] = deco_binary_ufunc(ufunc)", "for _ufunc_name in _binary:`r`n    ufunc = getattr(_binary_ufuncs_impl, _ufunc_name)`r`n    globals()[_ufunc_name] = deco_binary_ufunc(ufunc)")
    $content = $content.Replace("for name in _unary:`r`n    ufunc = getattr(_unary_ufuncs_impl, name)`r`n    vars()[name] = deco_unary_ufunc(ufunc)", "for _ufunc_name in _unary:`r`n    ufunc = getattr(_unary_ufuncs_impl, _ufunc_name)`r`n    globals()[_ufunc_name] = deco_unary_ufunc(ufunc)")
    Set-Content -LiteralPath $torchUfuncs -Value $content -Encoding UTF8
}

$torchFuncs = ".\.venv\Lib\site-packages\torch\_numpy\_funcs.py"
if (Test-Path -LiteralPath $torchFuncs) {
    $content = Get-Content -LiteralPath $torchFuncs -Raw
    $content = $content.Replace("    vars()[name] = decorated", "    globals()[name] = decorated")
    Set-Content -LiteralPath $torchFuncs -Value $content -Encoding UTF8
}

$torchDtypes = ".\.venv\Lib\site-packages\torch\_numpy\_dtypes.py"
if (Test-Path -LiteralPath $torchDtypes) {
    $content = Get-Content -LiteralPath $torchDtypes -Raw
    $content = $content.Replace("for name, obj in _name_aliases.items():`r`n    vars()[name] = obj", "for _alias_name, obj in _name_aliases.items():`r`n    globals()[_alias_name] = obj")
    Set-Content -LiteralPath $torchDtypes -Value $content -Encoding UTF8
}

Write-Host ""
Write-Host "Install complete in .venv. Run .\run.ps1 to start."
