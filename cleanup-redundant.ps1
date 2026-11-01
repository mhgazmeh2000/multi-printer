# cleanup-redundant.ps1
# PERMANENTLY deletes files that are not needed to run multi-printer.
#  - Dry run by default. Add -Execute, then type YES to confirm.
#  - Add -Yes to skip the typed confirmation (for unattended runs).
#  - Never deletes anything git tracks.
#  - Never deletes runtime data or config (.env, logs.db, printers.json, ...).
#  - Deletion is NOT reversible. Copy the folder first if unsure.
#
# Usage (run from the folder that CONTAINS multi-printer):
#   powershell -ExecutionPolicy Bypass -File .\cleanup-redundant.ps1 -RepoPath .\multi-printer
#   powershell -ExecutionPolicy Bypass -File .\cleanup-redundant.ps1 -RepoPath .\multi-printer -Execute

param(
    [Parameter(Mandatory = $true)] [string]$RepoPath,
    [switch]$Execute,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

$repo = (Resolve-Path -LiteralPath $RepoPath).Path
if (-not (Test-Path -LiteralPath (Join-Path $repo ".git"))) {
    throw "Not a git repository: $repo"
}
Set-Location -LiteralPath $repo

# Never deleted, even if someone adds them to $items below.
$never = @(
    ".env", "logs.db", "printers.json", "groups.json", "system_config.json",
    "sensor_alert_config.json", "cartridge_yield_catalog.json", "oid_profiles.json",
    "backups", ".venv", ".env.example", "make-env.ps1", "requirements.txt", "README.md", "README.en.md"
)

# Redundant: empty junk, temp files, generated outputs, patch files, duplicate docs, caches.
$items = @(
    # empty junk files created by a mistyped command
    "py", "3.1 --default-timeout=100", "!TEMP_LOG!",
    # leftover temp databases from the import route
    "temp_import_12636_1792828118725.db", "temp_import_3740_1792663633139.db",
    # generated test output and runtime reports (the app recreates them)
    "pytest_J_real.txt", "toner_report.txt", "missing_yield_printers.txt",
    "oid_validation_errors.txt", "caches_readiness.json", "logs.db.diagnosis.json",
    # patch files
    "captcha-bypass.patch", "captcha-changes.patch", "diagnose-sensor-default.patch",
    "fix-altcha-missing-secret.patch", "onboarding-login-401-fix.patch", "phaseJ.patch",
    # duplicate or obsolete docs and files
    "FILES.txt", "README-FA.txt", "CLEANUP_REPORT.md", "env.example",
    # leftover backup file from an earlier step
    "tests/test_captcha_bypass.py.bak",
    # regenerable caches
    "__pycache__", ".pytest_cache"
)

Write-Host ""
if ($Execute) {
    Write-Host "Mode: EXECUTE (files will be PERMANENTLY deleted)"
} else {
    Write-Host "Mode: DRY RUN (nothing will be deleted)"
}
Write-Host ""

# Build the list of what would actually be deleted.
$targets = New-Object System.Collections.Generic.List[string]
$skipped = 0

foreach ($item in $items) {
    if ($never -contains $item) {
        Write-Host ("  SKIP (protected)       " + $item)
        $skipped++
        continue
    }

    $full = Join-Path $repo $item
    if (-not (Test-Path -LiteralPath $full)) {
        continue
    }

    # Never delete anything git tracks.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    git ls-files --error-unmatch -- "$item" 2>$null | Out-Null
    $tracked = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    if ($tracked) {
        Write-Host ("  SKIP (tracked by git)  " + $item)
        $skipped++
        continue
    }

    $targets.Add($item)
    Write-Host ("  delete                 " + $item)
}

Write-Host ""
Write-Host ("To delete: " + $targets.Count + "   Skipped: " + $skipped)

if ($targets.Count -eq 0) {
    Write-Host "Nothing to delete."
    exit 0
}

if (-not $Execute) {
    Write-Host ""
    Write-Host "Nothing was deleted. Re-run with -Execute to delete these files."
    exit 0
}

if (-not $Yes) {
    Write-Host ""
    $answer = Read-Host "Type YES to permanently delete these files"
    if ($answer -ne "YES") {
        Write-Host "Cancelled. Nothing was deleted."
        exit 0
    }
}

$deleted = 0
foreach ($item in $targets) {
    $full = Join-Path $repo $item
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
        Write-Host ("  DELETED                " + $item)
        $deleted++
    }
}

Write-Host ""
Write-Host ("Done. Deleted: " + $deleted)
