#requires -Version 5.1
# SessionStart-hook handler for ai-video-factory skill repo.
# Pull latest before session loads so we work on up-to-date skill code.
# Silent on success; failures appended to .sync.log.

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
Set-Location -LiteralPath $repo

$log = Join-Path $repo '.sync.log'
$abortFile = Join-Path $repo '.rebase-abort-count'
function Log($msg) {
  $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

git pull --rebase --autostash origin main 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  if (Test-Path (Join-Path $repo '.git\rebase-merge')) {
    git rebase --abort 2>$null | Out-Null
    $n = 0
    if (Test-Path $abortFile) {
      try { $n = [int](((Get-Content -LiteralPath $abortFile -Raw -ErrorAction SilentlyContinue) -replace '\s+','')) } catch {}
    }
    $n += 1
    $n | Out-File -LiteralPath $abortFile -Encoding ascii -NoNewline
    Log "rebase conflict at SessionStart (#$n consecutive); aborted (manual cleanup may be needed)"
  } else {
    Log "SessionStart pull failed (offline?); using local state for this session"
  }
}
