#requires -Version 5.1
# Stop-hook handler for ai-video-factory skill repo.
# Mirrors memory/_sync.ps1: stage local changes, rebase on remote, push.
# Silent on success; failures go to .sync.log (gitignored).

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
Set-Location -LiteralPath $repo

$log = Join-Path $repo '.sync.log'
$abortFile = Join-Path $repo '.rebase-abort-count'
function Log($msg) {
  $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}
function Clear-AbortCount {
  if (Test-Path $abortFile) { Remove-Item $abortFile -ErrorAction SilentlyContinue }
}
function Bump-AbortCount {
  $n = 0
  if (Test-Path $abortFile) {
    try { $n = [int](((Get-Content -LiteralPath $abortFile -Raw -ErrorAction SilentlyContinue) -replace '\s+','')) } catch {}
  }
  $n += 1
  $n | Out-File -LiteralPath $abortFile -Encoding ascii -NoNewline
  return $n
}

# Catch up any unpushed commits from prior failed syncs first
$ahead = 0
try { $ahead = [int]((git rev-list origin/main..HEAD --count 2>$null) -replace '\s+','') } catch {}
if ($ahead -gt 0) {
  git pull --rebase --autostash origin main 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) {
    git push origin main 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Log "catch-up push failed (offline?); $ahead unpushed commits will retry"
      return
    }
    Log "catch-up: pushed $ahead previously-stuck commits"
    Clear-AbortCount
  } else {
    if (Test-Path (Join-Path $repo '.git\rebase-merge')) {
      git rebase --abort 2>$null | Out-Null
    }
    $n = Bump-AbortCount
    Log "rebase aborted during catch-up sync (#$n consecutive); $ahead local commits unsynced -- manual review needed if this persists"
    return
  }
}

# 1. Stage everything
git add -A 2>$null

# 2. Bail early if nothing changed
if (-not (git status --porcelain)) { return }

# 3. Commit
$msg = "skill auto-sync {0} ({1})" -f (Get-Date -Format 'yyyy-MM-dd HH:mm'), $env:COMPUTERNAME
git commit -m $msg 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Log "commit failed (status: $LASTEXITCODE)"; return }

# 4. Rebase on remote
git pull --rebase --autostash origin main 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  if (Test-Path (Join-Path $repo '.git\rebase-merge')) {
    git rebase --abort 2>$null | Out-Null
    $n = Bump-AbortCount
    Log "rebase conflict during sync (#$n consecutive); local commit kept unpushed"
  } else {
    Log "pull failed (offline?); local commit kept, will retry next turn"
  }
  return
}

# 5. Push
git push origin main 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Log "push failed (offline or auth?); local commit kept, will retry next turn"
  return
}

Clear-AbortCount
