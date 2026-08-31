$ErrorActionPreference = "Stop"
$skillDir = Split-Path -Parent $PSScriptRoot

function Find-PythonCommand {
    foreach ($name in @("py", "python")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) {
            continue
        }
        try {
            & $command.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) {
                return $command
            }
        } catch {
            continue
        }
    }
    return $null
}

function Require-Winget {
    $command = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Automatic setup requires Windows Package Manager (winget). Install App Installer from Microsoft Store and retry."
    }
    return $command
}

$pythonCmd = Find-PythonCommand
if (-not $pythonCmd) {
    $winget = Require-Winget
    & $winget.Source install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "Python 3.12 automatic installation failed."
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $pythonCmd = Find-PythonCommand
    if (-not $pythonCmd) {
        throw "Python was installed but is not visible yet. Restart Windows and double-click the installer again."
    }
}

$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
$ffprobeCmd = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffmpegCmd -or -not $ffprobeCmd) {
    $winget = Require-Winget
    & $winget.Source install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "FFmpeg automatic installation failed."
    }
}

& $pythonCmd.Source (Join-Path $PSScriptRoot "install.py") --root $skillDir --no-desktop-launcher
if ($LASTEXITCODE -ne 0) {
    throw "AI Video Factory automatic initialization failed."
}

& (Join-Path $PSScriptRoot "install_desktop_launcher.ps1") -SkillRoot $skillDir | Out-Null

& (Join-Path $PSScriptRoot "open_management.ps1")
