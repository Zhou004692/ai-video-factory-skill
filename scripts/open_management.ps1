$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $systemPython) {
        $systemPython = Get-Command py -ErrorAction SilentlyContinue
    }
    if (-not $systemPython) {
        throw "需要 Python 3.11 或更高版本才能启动 AI 视频工厂"
    }
    & $systemPython.Source (Join-Path $PSScriptRoot "install.py") --root $root *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "AI 视频工厂自动初始化失败"
    }
}
& $python (Join-Path $PSScriptRoot "install.py") --root $root --no-venv --no-deps *> $null
if ($LASTEXITCODE -ne 0) {
    throw "AI 视频工厂本地状态检查失败"
}
$envFile = Join-Path $root ".env"
$startPage = "overview"
if (-not (Test-Path -LiteralPath $envFile)) {
    $startPage = "config"
} elseif (Select-String -LiteralPath $envFile -Pattern "=<required>" -Quiet) {
    $startPage = "config"
}
& $python (Join-Path $PSScriptRoot "management_server.py") --start-page $startPage
