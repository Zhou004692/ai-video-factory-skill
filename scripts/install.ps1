$ErrorActionPreference = "Stop"
$skillDir = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $skillDir ".venv"

Write-Host "Skill root: $skillDir" -ForegroundColor Cyan

# 1. Python venv
if (-not (Test-Path $venvDir)) {
    Write-Host "Creating Python venv..." -ForegroundColor Cyan
    python -m venv $venvDir
}
$pip = Join-Path $venvDir "Scripts\pip.exe"
$python = Join-Path $venvDir "Scripts\python.exe"

# 2. dependencies
Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $pip install --upgrade pip -q
& $pip install -r (Join-Path $skillDir "scripts\requirements.txt") -q

# 3. init DB
Write-Host "Initializing SQLite..." -ForegroundColor Cyan
$initCode = "import sys; sys.path.insert(0, r'$($skillDir)\scripts'); from factory.db import conn; conn(); print('db ok')"
& $python -c $initCode

# 4. ffmpeg check
Write-Host "Checking ffmpeg..." -ForegroundColor Cyan
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) {
    Write-Warning "ffmpeg not found. Install: winget install Gyan.FFmpeg"
} else {
    Write-Host "  OK: ffmpeg at $($ff.Source)" -ForegroundColor Green
}

# 5. .env template
$envFile = Join-Path $skillDir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $skillDir "templates\env.example") $envFile
    Write-Host "  Generated .env template, please edit: $envFile" -ForegroundColor Yellow
} else {
    Write-Host "  .env already exists, not overwriting" -ForegroundColor Green
}

# 6. BGM library placeholder
$bgmDir = Join-Path $skillDir "references\bgm_library"
if (-not (Test-Path $bgmDir)) {
    New-Item -ItemType Directory -Force -Path $bgmDir | Out-Null
    foreach ($s in @("lo-fi", "ambient-piano", "synthwave", "chinese-guzheng", "hk-saxophone")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $bgmDir $s) | Out-Null
    }
    Write-Host "  Created bgm_library subdirs (drop mp3 files in by style)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Edit .env: notepad `"$envFile`""
Write-Host "  2. Smoke test:  `"$python`" `"$skillDir\scripts\cli.py`" status"
Write-Host "  3. Run a single topic:"
Write-Host "       `"$python`" `"$skillDir\scripts\cli.py`" run `"$skillDir\templates\test_single.csv`""
