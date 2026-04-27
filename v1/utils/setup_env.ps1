# ═══════════════════════════════════════════════════════════════════
#                BicoccaLab v7 - Environment Setup Script
# ═══════════════════════════════════════════════════════════════════
#
# This script sets up the Python virtual environment and installs
# all required dependencies for the BicoccaLab system.
#
# Usage:
#   .\setup_env.ps1              # Full setup (create venv + install deps)
#   .\setup_env.ps1 -SkipVenv    # Only install deps (venv already exists)
#   .\setup_env.ps1 -Force       # Recreate venv from scratch
#
# ═══════════════════════════════════════════════════════════════════

param(
    [switch]$SkipVenv,    # Skip venv creation, only install deps
    [switch]$Force,       # Force recreate venv even if exists
    [switch]$Verbose      # Show detailed output
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host ""
Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "          BicoccaLab v7 - Environment Setup" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""

# ───────────────────────────────────────────────────────────────────
# Step 1: Check Python installation
# ───────────────────────────────────────────────────────────────────
Write-Host "[1/5] Checking Python installation..." -ForegroundColor Yellow

$pythonCmd = $null
$pythonVersion = $null

# Try different python commands
foreach ($cmd in @("python", "python3", "py -3")) {
    try {
        $version = & $cmd.Split()[0] $cmd.Split()[1..$cmd.Split().Length] --version 2>&1
        if ($version -match "Python (\d+\.\d+)") {
            $ver = [version]$Matches[1]
            if ($ver -ge [version]"3.10") {
                $pythonCmd = $cmd
                $pythonVersion = $version
                break
            }
        }
    } catch {
        continue
    }
}

if (-not $pythonCmd) {
    Write-Host "ERROR: Python 3.10+ not found. Please install Python first." -ForegroundColor Red
    Write-Host "Download from: https://www.python.org/downloads/" -ForegroundColor Gray
    exit 1
}

Write-Host "  Found: $pythonVersion" -ForegroundColor Green

# ───────────────────────────────────────────────────────────────────
# Step 2: Create/Recreate Virtual Environment
# ───────────────────────────────────────────────────────────────────
$venvPath = Join-Path $ProjectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$venvPip = Join-Path $venvPath "Scripts\pip.exe"
$venvActivate = Join-Path $venvPath "Scripts\Activate.ps1"

if (-not $SkipVenv) {
    Write-Host ""
    Write-Host "[2/5] Setting up virtual environment..." -ForegroundColor Yellow
    
    # Check if venv exists and is valid
    $venvValid = $false
    if (Test-Path $venvPython) {
        try {
            # Verify venv points to correct Python
            $venvConfig = Get-Content (Join-Path $venvPath "pyvenv.cfg") -Raw
            if ($venvConfig -match "home = (.+)") {
                $venvHome = $Matches[1].Trim()
                if (Test-Path $venvHome) {
                    $venvValid = $true
                }
            }
        } catch {
            $venvValid = $false
        }
    }
    
    if ($Force -or -not $venvValid) {
        if (Test-Path $venvPath) {
            Write-Host "  Removing existing (invalid/forced) venv..." -ForegroundColor Gray
            Remove-Item $venvPath -Recurse -Force -ErrorAction SilentlyContinue
        }
        
        Write-Host "  Creating new virtual environment..." -ForegroundColor Gray
        & $pythonCmd.Split()[0] $pythonCmd.Split()[1..$pythonCmd.Split().Length] -m venv $venvPath
        
        if (-not (Test-Path $venvPython)) {
            Write-Host "ERROR: Failed to create virtual environment" -ForegroundColor Red
            exit 1
        }
        Write-Host "  Virtual environment created" -ForegroundColor Green
    } else {
        Write-Host "  Virtual environment exists and is valid" -ForegroundColor Green
    }
} else {
    Write-Host ""
    Write-Host "[2/5] Skipping venv creation (--SkipVenv)" -ForegroundColor Gray
}

# ───────────────────────────────────────────────────────────────────
# Step 3: Upgrade pip
# ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/5] Upgrading pip..." -ForegroundColor Yellow

& $venvPython -m pip install --upgrade pip --quiet
Write-Host "  pip upgraded" -ForegroundColor Green

# ───────────────────────────────────────────────────────────────────
# Step 4: Install dependencies
# ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[4/5] Installing dependencies..." -ForegroundColor Yellow

$requirementsFile = Join-Path $ProjectRoot "requirements.txt"
if (Test-Path $requirementsFile) {
    if ($Verbose) {
        & $venvPip install -r $requirementsFile
    } else {
        & $venvPip install -r $requirementsFile --quiet
    }
    Write-Host "  All dependencies installed" -ForegroundColor Green
} else {
    Write-Host "  WARNING: requirements.txt not found" -ForegroundColor Yellow
}

# ───────────────────────────────────────────────────────────────────
# Step 5: Verify installation
# ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[5/5] Verifying installation..." -ForegroundColor Yellow

$verifyScript = @"
import sys
errors = []

# Check core dependencies
deps = [
    ('grpcio', 'grpc'),
    ('httpx', 'httpx'),
    ('pyyaml', 'yaml'),
    ('fastapi', 'fastapi'),
    ('uvicorn', 'uvicorn'),
    ('jinja2', 'jinja2'),
    ('zeroconf', 'zeroconf'),
    ('colorama', 'colorama'),
]

for pkg_name, import_name in deps:
    try:
        __import__(import_name)
        print(f'  [OK] {pkg_name}')
    except ImportError as e:
        print(f'  [FAIL] {pkg_name}: {e}')
        errors.append(pkg_name)

sys.exit(len(errors))
"@

$result = & $venvPython -c $verifyScript

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Green
    Write-Host "  Setup completed successfully!" -ForegroundColor Green
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Yellow
    Write-Host "  Setup completed with warnings. Some packages may need manual install." -ForegroundColor Yellow
    Write-Host "═══════════════════════════════════════════════════════════════════" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "To activate the environment, run:" -ForegroundColor Cyan
Write-Host "  .\.venv\Scripts\Activate.ps1" -ForegroundColor White
Write-Host ""
Write-Host "To start the system, run:" -ForegroundColor Cyan
Write-Host "  .\START.ps1" -ForegroundColor White
Write-Host ""
