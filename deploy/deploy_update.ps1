<#
.SYNOPSIS
    NovaBak Deployment Script — deploys updated files from ZIP to production server.

.USAGE
    1. Copy deploy_update.ps1 and novabak_update.zip to the server
    2. Run: .\deploy_update.ps1
#>

param(
    [string]$TargetDir = (Split-Path -Parent $PSScriptRoot),
    [string]$VenvPython = (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"),
    [switch]$NoRestart,
    [switch]$Force
)

$ErrorActionPreference = "Continue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$zipFile = Join-Path $scriptDir "novabak_update.zip"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupDir = Join-Path $TargetDir "_backups\$timestamp"

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "    NovaBak Update Deployment" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""

# -- Step 0: Validate --
if (-not (Test-Path $zipFile)) {
    Write-Host "[ERROR] novabak_update.zip not found next to this script!" -ForegroundColor Red
    Write-Host "        Expected: $zipFile" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $TargetDir)) {
    Write-Host "[ERROR] Target directory not found: $TargetDir" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $VenvPython)) {
    Write-Host "[ERROR] Python venv not found: $VenvPython" -ForegroundColor Red
    exit 1
}

Write-Host "[INFO] Target:  $TargetDir" -ForegroundColor Gray
Write-Host "[INFO] ZIP:     $zipFile" -ForegroundColor Gray
Write-Host "[INFO] Backup:  $backupDir" -ForegroundColor Gray
Write-Host ""

# -- Known files to deploy --
$deployFiles = @(
    "backup_engine.py",
    "main.py",
    "worker.py",
    "requirements.txt",
    "templates\index.html",
    "templates\partials\overview_tab.html",
    "templates\partials\settings_tab.html"
)

# -- Step 1: Check for running backups --
Write-Host "[1/5] Checking for active backups..." -ForegroundColor Yellow
try {
    $checkScript = @"
import sqlite3, sys
try:
    conn = sqlite3.connect(r'$TargetDir\data\backup_system.db')
    rows = conn.execute("SELECT vm_name FROM backup_jobs WHERE status='Running'").fetchall()
    conn.close()
    if rows:
        print('RUNNING:' + ','.join(r[0] for r in rows))
    else:
        print('NONE')
except:
    print('NONE')
"@
    $result = $checkScript | & $VenvPython - 2>$null
    if ($result -match "^RUNNING:") {
        $vms = ($result -replace "^RUNNING:","")
        Write-Host "      [WARNING] Active backups: $vms" -ForegroundColor Red
        if (-not $Force) {
            $answer = Read-Host "Continue anyway? (y/N)"
            if ($answer -ne "y") {
                Write-Host "Aborted." -ForegroundColor Yellow
                exit 0
            }
        }
    } else {
        Write-Host "      No active backups." -ForegroundColor Green
    }
} catch {
    Write-Host "      Could not check. Continuing..." -ForegroundColor Yellow
}

# -- Step 2: Backup current files --
Write-Host "[2/5] Backing up current files..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

foreach ($f in $deployFiles) {
    $src = Join-Path $TargetDir $f
    if (Test-Path $src) {
        $dest = Join-Path $backupDir $f
        $destDir = Split-Path -Parent $dest
        if (-not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }
        Copy-Item $src $dest -Force
        Write-Host "      Backed up: $f" -ForegroundColor Gray
    }
}
Write-Host "      Backup saved to: $backupDir" -ForegroundColor Green

# -- Step 3: Extract ZIP and deploy --
Write-Host "[3/5] Deploying updated files..." -ForegroundColor Yellow
$extractDir = Join-Path $env:TEMP "novabak_update_$timestamp"
if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
Expand-Archive -Path $zipFile -DestinationPath $extractDir -Force

$deployed = 0
foreach ($f in $deployFiles) {
    # Search for the file in the extract directory (handles nested folders)
    $baseName = Split-Path -Leaf $f
    $found = Get-ChildItem -Path $extractDir -Recurse -File -Filter $baseName | Select-Object -First 1
    
    if ($found) {
        $destFile = Join-Path $TargetDir $f
        $destDir = Split-Path -Parent $destFile
        if (-not (Test-Path $destDir)) {
            New-Item -ItemType Directory -Path $destDir -Force | Out-Null
        }
        Copy-Item $found.FullName $destFile -Force
        Write-Host "      Updated: $f" -ForegroundColor Gray
        $deployed++
    } else {
        Write-Host "      [SKIP] Not in ZIP: $f" -ForegroundColor Yellow
    }
}
Write-Host "      $deployed files deployed." -ForegroundColor Green

# Cleanup temp
Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue

# -- Step 4: Install dependencies --
Write-Host "[4/5] Installing new dependencies..." -ForegroundColor Yellow
$pip = Join-Path (Split-Path $VenvPython) "pip.exe"
try {
    $pipOutput = & $pip install zstandard --quiet 2>&1
    Write-Host "      zstandard installed." -ForegroundColor Green
} catch {
    Write-Host "      [WARNING] pip had an issue. Trying alternative..." -ForegroundColor Yellow
    try {
        & $VenvPython -m pip install zstandard --quiet 2>&1 | Out-Null
        Write-Host "      zstandard installed (via python -m pip)." -ForegroundColor Green
    } catch {
        Write-Host "      [WARNING] Install manually: .venv\Scripts\pip install zstandard" -ForegroundColor Yellow
    }
}

# -- Step 5: Restart services --
if ($NoRestart) {
    Write-Host "[5/5] Skipping restart (--NoRestart flag)." -ForegroundColor Yellow
} else {
    Write-Host "[5/5] Restarting services..." -ForegroundColor Yellow
    
    # Kill existing Python processes
    $procs = Get-Process python -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Host "      Stopping $($procs.Count) Python process(es)..." -ForegroundColor Gray
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep 3
    }

    # Start Web UI
    Start-Process -FilePath $VenvPython -ArgumentList "main.py" -WorkingDirectory $TargetDir -WindowStyle Minimized
    Write-Host "      Started: main.py (Web UI)" -ForegroundColor Green
    Start-Sleep 2

    # Start Worker
    Start-Process -FilePath $VenvPython -ArgumentList "worker_daemon.py" -WorkingDirectory $TargetDir -WindowStyle Minimized
    Write-Host "      Started: worker_daemon.py (Backup Engine)" -ForegroundColor Green
}

Write-Host ""
Write-Host "=========================================" -ForegroundColor Green
Write-Host "    Deployment complete!" -ForegroundColor Green
Write-Host "=========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Rollback command:" -ForegroundColor DarkGray
Write-Host "  Copy-Item '$backupDir\*' '$TargetDir\' -Recurse -Force" -ForegroundColor DarkGray
Write-Host ""
