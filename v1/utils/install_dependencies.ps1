# ═══════════════════════════════════════════════════════════════════
#                BicoccaLab v7 - Dependency Installer
# ═══════════════════════════════════════════════════════════════════
#
# This script installs all dependencies for BicoccaLab.
# Run this AFTER running setup_env.ps1 to create the venv.
#
# Usage:
#   .\install_dependencies.ps1              # Install Python deps
#   .\install_dependencies.ps1 -All         # Install Python + .NET SDK
#   .\install_dependencies.ps1 -DotnetOnly  # Install only .NET SDK
#
# ═══════════════════════════════════════════════════════════════════

param(
    [switch]$All,           # Install everything (Python + .NET)
    [switch]$DotnetOnly,    # Only install .NET SDK
    [switch]$Offline,       # Try offline installation from cache
    [switch]$Verbose        # Show detailed output
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host ""
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host "          BicoccaLab v7 - Dependency Installer" -ForegroundColor Cyan
Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host ""

# ───────────────────────────────────────────────────────────────────
# Test network connectivity
# ───────────────────────────────────────────────────────────────────
function Test-NetworkConnection {
    Write-Host "[*] Testing network connectivity..." -ForegroundColor Yellow
    
    $hasConnection = $false
    
    # Test PyPI
    try {
        $result = Test-Connection -ComputerName "pypi.org" -Count 1 -Quiet -ErrorAction SilentlyContinue
        if ($result) {
            Write-Host "  [OK] PyPI - reachable" -ForegroundColor Green
            $hasConnection = $true
        } else {
            Write-Host "  [FAIL] PyPI - not reachable" -ForegroundColor Red
        }
    } catch {
        Write-Host "  [FAIL] PyPI - error" -ForegroundColor Red
    }
    
    # Test Google DNS
    try {
        $result = Test-Connection -ComputerName "8.8.8.8" -Count 1 -Quiet -ErrorAction SilentlyContinue
        if ($result) {
            Write-Host "  [OK] Google DNS - reachable" -ForegroundColor Green
            $hasConnection = $true
        } else {
            Write-Host "  [FAIL] Google DNS - not reachable" -ForegroundColor Red
        }
    } catch {
        Write-Host "  [FAIL] Google DNS - error" -ForegroundColor Red
    }
    
    if (-not $hasConnection) {
        Write-Host ""
        Write-Host "  WARNING: No network connection detected!" -ForegroundColor Yellow
        Write-Host "  Please check:" -ForegroundColor Gray
        Write-Host "    - WiFi/Ethernet connection" -ForegroundColor Gray
        Write-Host "    - Firewall settings" -ForegroundColor Gray
        Write-Host "    - Proxy configuration" -ForegroundColor Gray
        Write-Host ""
    }
    
    return $hasConnection
}

# ───────────────────────────────────────────────────────────────────
# Install Python dependencies
# ───────────────────────────────────────────────────────────────────
function Install-PythonDeps {
    Write-Host ""
    Write-Host "[1/2] Installing Python dependencies..." -ForegroundColor Yellow
    
    $venvPip = Join-Path $ProjectRoot ".venv\Scripts\pip.exe"
    
    if (-not (Test-Path $venvPip)) {
        Write-Host "  [ERROR] Virtual environment not found!" -ForegroundColor Red
        Write-Host "  Run .\setup_env.ps1 first" -ForegroundColor Gray
        return $false
    }
    
    # Core dependencies (ordered by importance)
    $packages = @(
        "grpcio>=1.60.0",
        "grpcio-tools>=1.60.0",
        "grpcio-reflection>=1.60.0",
        "fastapi>=0.110.0",
        "uvicorn[standard]>=0.27.0",
        "httpx>=0.27.0",
        "aiofiles>=23.2.1",
        "pyyaml>=6.0.1",
        "python-dotenv>=1.0.0",
        "pydantic>=2.6.0",
        "zeroconf>=0.131.0",
        "rich>=13.7.0",
        "click>=8.1.7"
    )
    
    $successCount = 0
    $failedPackages = @()
    
    foreach ($pkg in $packages) {
        $pkgName = $pkg -replace ">=.*|==.*|\[.*\]", ""
        Write-Host "  Installing $pkgName..." -ForegroundColor Gray -NoNewline
        
        $output = & $venvPip install $pkg --quiet 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Host " OK" -ForegroundColor Green
            $successCount++
        } else {
            Write-Host " FAILED" -ForegroundColor Red
            $failedPackages += $pkgName
        }
    }
    
    # Summary
    Write-Host ""
    if ($failedPackages.Count -eq 0) {
        Write-Host "  Installed: $successCount/$($packages.Count) packages" -ForegroundColor Green
    } else {
        Write-Host "  Installed: $successCount/$($packages.Count) packages" -ForegroundColor Yellow
        Write-Host "  Failed packages: $($failedPackages -join ', ')" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Try installing failed packages manually:" -ForegroundColor Gray
        Write-Host "    .\.venv\Scripts\activate" -ForegroundColor Cyan
        Write-Host "    pip install PACKAGE_NAME" -ForegroundColor Cyan
    }
    
    return ($failedPackages.Count -eq 0)
}

# ───────────────────────────────────────────────────────────────────
# Check and install .NET SDK
# ───────────────────────────────────────────────────────────────────
function Install-DotnetSDK {
    Write-Host ""
    Write-Host "[2/2] Checking .NET SDK..." -ForegroundColor Yellow
    
    # Check if dotnet is already installed
    try {
        $dotnetVersion = & dotnet --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $dotnetVersion -match "^\d+\.\d+") {
            Write-Host "  [OK] .NET SDK already installed: $dotnetVersion" -ForegroundColor Green
            return $true
        }
    } catch {
        # Not installed
    }
    
    Write-Host "  .NET SDK not found" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  The Tecan SiLA2 Server requires .NET SDK 8.0+" -ForegroundColor Cyan
    Write-Host ""
    
    # Try to download and install automatically
    $downloadUrl = "https://dot.net/v1/dotnet-install.ps1"
    $installScript = Join-Path $env:TEMP "dotnet-install.ps1"
    
    Write-Host "  Would you like to install .NET SDK 8.0 now? [Y/N]" -ForegroundColor Yellow
    $response = Read-Host "  "
    
    if ($response -eq "Y" -or $response -eq "y") {
        Write-Host "  Downloading .NET installer..." -ForegroundColor Gray
        
        try {
            # Download installer script
            Invoke-WebRequest -Uri $downloadUrl -OutFile $installScript -UseBasicParsing
            
            Write-Host "  Installing .NET SDK 8.0..." -ForegroundColor Gray
            $dotnetInstallDir = "$env:LOCALAPPDATA\Microsoft\dotnet"
            & $installScript -Channel 8.0 -InstallDir $dotnetInstallDir
            
            # Add to PATH for current session
            if (-not ($env:PATH -split ";" | Where-Object { $_ -eq $dotnetInstallDir })) {
                $env:PATH = $dotnetInstallDir + ";" + $env:PATH
            }
            
            Write-Host "  [OK] .NET SDK installed successfully" -ForegroundColor Green
            Write-Host ""
            Write-Host "  WARNING: You may need to restart your terminal for dotnet to be available" -ForegroundColor Yellow
            
            return $true
            
        } catch {
            Write-Host "  [ERROR] Failed to install .NET SDK automatically" -ForegroundColor Red
            Write-Host "  Please install manually from: https://dotnet.microsoft.com/download" -ForegroundColor Gray
            return $false
        }
    } else {
        Write-Host ""
        Write-Host "  To install .NET SDK manually:" -ForegroundColor Gray
        Write-Host "    1. Go to https://dotnet.microsoft.com/download" -ForegroundColor Cyan
        Write-Host "    2. Download .NET 8.0 SDK" -ForegroundColor Cyan
        Write-Host "    3. Run the installer" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  The Tecan server will not work without .NET SDK." -ForegroundColor Yellow
        return $false
    }
}

# ───────────────────────────────────────────────────────────────────
# Main execution
# ───────────────────────────────────────────────────────────────────

# Test network first
$hasNetwork = Test-NetworkConnection

if (-not $hasNetwork) {
    Write-Host "  Continue anyway? [Y/N]" -ForegroundColor Yellow
    $continue = Read-Host "  "
    if ($continue -ne "Y" -and $continue -ne "y") {
        Write-Host "  Aborted." -ForegroundColor Gray
        exit 1
    }
}

$pythonSuccess = $true
$dotnetSuccess = $true

if (-not $DotnetOnly) {
    $pythonSuccess = Install-PythonDeps
}

if ($All -or $DotnetOnly) {
    $dotnetSuccess = Install-DotnetSDK
}

# Final summary
Write-Host ""
Write-Host "=======================================================================" -ForegroundColor Cyan

if ($pythonSuccess -and $dotnetSuccess) {
    Write-Host "  [OK] All dependencies installed successfully!" -ForegroundColor Green
} else {
    Write-Host "  [WARNING] Some dependencies failed to install." -ForegroundColor Yellow
    Write-Host "  Check the output above for details." -ForegroundColor Gray
}

Write-Host "=======================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  To activate the environment:" -ForegroundColor Gray
Write-Host "    .\.venv\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Host "  To start the system:" -ForegroundColor Gray
Write-Host "    .\START.ps1" -ForegroundColor Cyan
Write-Host ""
