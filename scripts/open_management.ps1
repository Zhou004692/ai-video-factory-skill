param(
    [switch]$NoBrowser,
    [int]$WaitSeconds = 60,
    [int]$Port = 8765,
    [string]$SkillRoot = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($SkillRoot)) {
    $SkillRoot = Split-Path -Parent $PSScriptRoot
}
$root = [System.IO.Path]::GetFullPath($SkillRoot)
$baseUrl = "http://127.0.0.1:$Port"
$readyUrl = "$baseUrl/readyz"

function Test-ManagementReady {
    try {
        $response = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Open-ManagementPage([string]$startPage) {
    if (-not $NoBrowser) {
        Start-Process "$baseUrl/#$startPage" | Out-Null
    }
}

function Show-StartupError([string]$message) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shell.Popup($message, 0, "AI Video Factory", 16) | Out-Null
    } catch {
    }
}

function Get-RuntimeHome {
    if ($env:FACTORY_RUNTIME_HOME) {
        return [System.IO.Path]::GetFullPath($env:FACTORY_RUNTIME_HOME)
    } elseif ($env:LOCALAPPDATA) {
        return Join-Path $env:LOCALAPPDATA "AI Video Factory"
    } elseif ($env:APPDATA) {
        return Join-Path $env:APPDATA "AI Video Factory"
    } else {
        return Join-Path (Split-Path -Parent $root) "AI Video Factory"
    }
}

function Get-RuntimeEnvFile {
    if ($env:FACTORY_ENV_FILE) {
        return [System.IO.Path]::GetFullPath($env:FACTORY_ENV_FILE)
    }
    return Join-Path (Get-RuntimeHome) ".env"
}

function Get-StartPage {
    $runtimeEnv = Get-RuntimeEnvFile
    if (-not (Test-Path -LiteralPath $runtimeEnv)) {
        return "config"
    }
    if (Select-String -LiteralPath $runtimeEnv -Pattern "=<required>" -Quiet) {
        return "config"
    }
    return "overview"
}

$startPage = Get-StartPage

if (Test-ManagementReady) {
    Open-ManagementPage $startPage
    exit 0
}

$logRoot = Join-Path (Get-RuntimeHome) "logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stdoutLog = Join-Path $logRoot "management-server.log"
$stderrLog = Join-Path $logRoot "management-server-error.log"
$launcherLog = Join-Path $logRoot "launcher-error.log"
$lockPath = Join-Path $logRoot "management-start.lock"
$lockStream = $null

try {
    try {
        $lockStream = [System.IO.File]::Open(
            $lockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $WaitSeconds))
        while ([DateTime]::UtcNow -lt $deadline) {
            if (Test-ManagementReady) {
                Open-ManagementPage $startPage
                exit 0
            }
            Start-Sleep -Milliseconds 400
        }
        throw "Another launcher is active, but the service did not become ready in time."
    }

    if (Test-ManagementReady) {
        Open-ManagementPage $startPage
        exit 0
    }

    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $systemPython = Get-Command python -ErrorAction SilentlyContinue
        if (-not $systemPython) {
            $systemPython = Get-Command py -ErrorAction SilentlyContinue
        }
        if (-not $systemPython) {
            throw "Python 3.11 or newer is required to start AI Video Factory."
        }
        & $systemPython.Source (Join-Path $PSScriptRoot "install.py") --root $root
        if ($LASTEXITCODE -ne 0) {
            throw "AI Video Factory automatic initialization failed."
        }
    }

    & $python (Join-Path $PSScriptRoot "install.py") --root $root --no-venv --no-deps *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "AI Video Factory local state validation failed."
    }
    $startPage = Get-StartPage

    $processOptions = @{
        FilePath = $python
        ArgumentList = @(
            "scripts\management_server.py",
            "--host", "127.0.0.1",
            "--port", "$Port",
            "--start-page", $startPage,
            "--no-browser"
        )
        WorkingDirectory = $root
        WindowStyle = "Hidden"
        RedirectStandardOutput = $stdoutLog
        RedirectStandardError = $stderrLog
        PassThru = $true
    }
    $process = Start-Process @processOptions

    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $WaitSeconds))
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-ManagementReady) {
            Open-ManagementPage $startPage
            exit 0
        }
        if ($process.HasExited) {
            throw "The management service exited before it became ready. See $stderrLog"
        }
        Start-Sleep -Milliseconds 400
    }
    throw "The management service did not become ready within $WaitSeconds seconds. See $stderrLog"
} catch {
    $detail = "$(Get-Date -Format o) $($_.Exception.Message)"
    Add-Content -LiteralPath $launcherLog -Value $detail -Encoding UTF8
    Show-StartupError "$($_.Exception.Message)`n`nLog: $launcherLog"
    exit 1
} finally {
    if ($lockStream) {
        $lockStream.Dispose()
    }
}
