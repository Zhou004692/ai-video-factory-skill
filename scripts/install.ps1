$ErrorActionPreference = "Stop"
$skillDir = Split-Path -Parent $PSScriptRoot

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $pythonCmd) {
    throw "Python 3.11+ is required. Install Python, then rerun scripts/install.ps1."
}

& $pythonCmd.Source (Join-Path $PSScriptRoot "install.py") --root $skillDir
if ($LASTEXITCODE -ne 0) {
    throw "AI 视频工厂自动初始化失败"
}

& (Join-Path $PSScriptRoot "open_management.ps1")
