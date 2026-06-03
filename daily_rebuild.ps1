# Local daily rebuild - runs build.py against log.xlsx (real basket) and pushes
# the refreshed docs/index.html + docs/data/payload.json (v2.0 sidecar) +
# data/ caches to GitHub.
#
# Intended to be triggered by Windows Task Scheduler ~once a day, after the
# GitHub Actions cron has run (08:00 UTC) so this rebuild benefits from the
# bot's pre-warmed data/ parquet caches.
#
# Failure handling: every external command's stderr is captured into the log;
# build failures abort the run without committing partial state.
#
# All characters in this file are plain ASCII so Windows PowerShell 5.1
# (which defaults to ANSI for BOM-less files) parses it without issues.

$ErrorActionPreference = "Continue"
$repo = $PSScriptRoot
$log  = Join-Path $repo "daily_rebuild.log"
$ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Write-Log($msg) {
    Add-Content -Path $log -Value "$msg"
}

Write-Log ""
Write-Log "=== $ts START ==="
Set-Location $repo

# 0. Pre-flight: pull --rebase refuses to run if the working tree has any
#    unstaged changes. Classify what is dirty:
#      - SAFE to discard: docs/index.html, docs/data/* (v2.0 sidecar payload),
#        data/*, daily_rebuild.log
#        (the script regenerates these anyway)
#      - UNSAFE: anything else (build.py, README, .gitignore, etc.)
#        Aborting protects in-progress code edits from being silently wiped.
Write-Log "--- pre-flight: classify dirty files ---"
$dirty = git status --porcelain
$unsafe = @()
$safe   = @()
foreach ($line in $dirty) {
    if (-not $line) { continue }
    # Porcelain format: "XY path" where X/Y are status chars and path follows.
    $path = $line.Substring(3).Trim()
    $isSafe = ($path -eq "docs/index.html") `
              -or ($path -like "docs/data/*") `
              -or ($path -like "data/*") `
              -or ($path -eq "daily_rebuild.log")
    if ($isSafe) { $safe += $path } else { $unsafe += $path }
}
if ($unsafe.Count -gt 0) {
    Write-Log "$ts ABORT - unexpected dirty files (commit or stash them manually):"
    $unsafe | ForEach-Object { Write-Log "  $_" }
    exit 1
}
if ($safe.Count -gt 0) {
    Write-Log "discarding stale build outputs (will be regenerated):"
    $safe | ForEach-Object { Write-Log "  $_" }
    git checkout -- docs/index.html docs/data/ data/ 2>&1 | ForEach-Object { Write-Log $_ }
}

# 1. Pull bot's overnight data/ refresh and demo.html rebuild
Write-Log "--- git pull --rebase ---"
$pullOut = git pull --rebase 2>&1
$pullOut | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "$ts PULL FAILED - aborting (resolve manually next time you are at the keyboard)"
    exit 1
}

# 2. Rebuild docs/index.html from log.xlsx
Write-Log "--- python build.py ---"
$buildOut = python build.py 2>&1
$buildOut | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "$ts BUILD FAILED - exit code $LASTEXITCODE; nothing committed"
    exit 1
}

# 3. Stage only the files that should change from a real-data rebuild
#    v2.0: docs/data/payload.json (modal-only fields, ~1 MB sidecar) is
#    written by every build and must stay in sync with docs/index.html's
#    cache-bust query (?v={build_timestamp}). Staging the directory means
#    new sidecars get picked up automatically without further edits here.
Write-Log "--- staging docs/index.html docs/data/ data/ ---"
git add docs/index.html docs/data/ data/ 2>&1 | ForEach-Object { Write-Log $_ }

# 4. Skip the commit if nothing changed (e.g. weekend with no price moves)
$staged = git diff --staged --name-only
if (-not $staged) {
    Write-Log "$ts no changes after rebuild - exiting clean"
    Write-Log "=== $ts DONE (no-op) ==="
    exit 0
}
Write-Log "files staged:"
$staged | ForEach-Object { Write-Log "  $_" }

# 5. Commit + push
Write-Log "--- git commit ---"
git commit -m "Local rebuild $ts" 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "$ts COMMIT FAILED - exit code $LASTEXITCODE"
    exit 1
}

Write-Log "--- git push ---"
git push 2>&1 | ForEach-Object { Write-Log $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "$ts PUSH FAILED - local commit exists but not published; run 'git push' manually next time"
    exit 1
}

Write-Log "=== $ts DONE ==="
exit 0
