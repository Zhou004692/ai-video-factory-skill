$ErrorActionPreference = "Stop"
$skillDir = Split-Path -Parent $PSScriptRoot
$venvDir = Join-Path $skillDir ".venv"

Write-Host "→ Skill 根目录: $skillDir" -ForegroundColor Cyan

# 1. Python venv
if (-not (Test-Path $venvDir)) {
    Write-Host "→ 创建 Python 虚拟环境..." -ForegroundColor Cyan
    python -m venv $venvDir
}
$pip = Join-Path $venvDir "Scripts\pip.exe"
$python = Join-Path $venvDir "Scripts\python.exe"

# 2. 依赖
Write-Host "→ 安装依赖..." -ForegroundColor Cyan
& $pip install --upgrade pip -q
& $pip install -r (Join-Path $skillDir "scripts\requirements.txt") -q

# 3. 初始化 DB
Write-Host "→ 初始化 SQLite..." -ForegroundColor Cyan
$initCode = "import sys; sys.path.insert(0, r'$($skillDir)\scripts'); from factory.db import conn; conn(); print('db ok')"
& $python -c $initCode

# 4. 检查 ffmpeg
Write-Host "→ 检查 ffmpeg..." -ForegroundColor Cyan
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ff) {
    Write-Warning "ffmpeg 未安装。请执行: winget install Gyan.FFmpeg"
} else {
    Write-Host "  ✓ ffmpeg: $($ff.Source)" -ForegroundColor Green
}

# 5. 复制 .env 模板
$envFile = Join-Path $skillDir ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $skillDir "templates\env.example") $envFile
    Write-Host "  ✓ 已生成 .env 模板,请编辑填入 API key: $envFile" -ForegroundColor Yellow
} else {
    Write-Host "  ✓ .env 已存在,未覆盖" -ForegroundColor Green
}

# 6. 创建 BGM library 占位
$bgmDir = Join-Path $skillDir "references\bgm_library"
if (-not (Test-Path $bgmDir)) {
    New-Item -ItemType Directory -Force -Path $bgmDir | Out-Null
    foreach ($s in @("lo-fi", "ambient piano", "synthwave", "中国风古筝", "港乐萨克斯")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $bgmDir $s) | Out-Null
    }
    Write-Host "  ✓ 已创建 bgm_library 子目录,请按风格放 mp3" -ForegroundColor Yellow
}

Write-Host "`n✓ 安装完成" -ForegroundColor Green
Write-Host "`n下一步:" -ForegroundColor Cyan
Write-Host "  1. 编辑 $envFile 填入 API key"
Write-Host "  2. 测试运行: & '$python' '$skillDir\scripts\cli.py' status"
Write-Host "  3. 跑一批: & '$python' '$skillDir\scripts\cli.py' run '$skillDir\templates\topics.csv.example'"
