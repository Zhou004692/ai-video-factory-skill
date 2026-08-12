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
    throw "AI Video Factory installation failed with exit code $LASTEXITCODE"
}

$venvPython = Join-Path $skillDir ".venv\Scripts\python.exe"
Write-Host "Run readiness check:" -ForegroundColor Cyan
Write-Host "  & `"$venvPython`" `"$PSScriptRoot\doctor.py`" --profile script"
Write-Host "  & `"$venvPython`" `"$PSScriptRoot\doctor.py`" --profile video"
