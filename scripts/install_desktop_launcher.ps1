param(
    [string]$SkillRoot = "",
    [string]$DesktopDirectory = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SkillRoot)) {
    $SkillRoot = Split-Path -Parent $PSScriptRoot
}
$root = [System.IO.Path]::GetFullPath($SkillRoot)
$managementLauncher = Join-Path $root "scripts\open_management.ps1"
if (-not (Test-Path -LiteralPath $managementLauncher -PathType Leaf)) {
    throw "AI Video Factory launcher is missing: $managementLauncher"
}

if ([string]::IsNullOrWhiteSpace($DesktopDirectory)) {
    $DesktopDirectory = [Environment]::GetFolderPath("Desktop")
}
if ([string]::IsNullOrWhiteSpace($DesktopDirectory)) {
    throw "Windows desktop directory could not be resolved"
}
$desktop = [System.IO.Path]::GetFullPath($DesktopDirectory)
if (-not (Test-Path -LiteralPath $desktop -PathType Container)) {
    New-Item -ItemType Directory -Path $desktop -Force | Out-Null
}

$launcherPath = Join-Path $desktop "AI Video Factory.cmd"
$escapedManagementLauncher = $managementLauncher.Replace("%", "%%")
$content = @"
@echo off
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "$escapedManagementLauncher"
exit /b 0
"@
[System.IO.File]::WriteAllText(
    $launcherPath,
    $content,
    [System.Text.UTF8Encoding]::new($false)
)
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Windows did not persist the desktop launcher: $launcherPath"
}

Write-Output $launcherPath
