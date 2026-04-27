# ═══════════════════════════════════════════════════════════════════════════
#                          BicoccaLab LAUNCHER
#              Double-click this file to start the system!
# ═══════════════════════════════════════════════════════════════════════════

param(
    [switch]$All,      # Start everything
    [switch]$Servers,  # Start servers only
    [switch]$WebApp,   # Start webapp only
    [switch]$CLI,      # Start CLI only
    [switch]$Status    # Show status only
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Activate virtual environment if exists
if (Test-Path ".venv\Scripts\Activate.ps1") {
    & ".venv\Scripts\Activate.ps1"
}

# Build arguments
$args = @()
if ($All) { $args += "--all" }
if ($Servers) { $args += "--servers" }
if ($WebApp) { $args += "--webapp" }
if ($CLI) { $args += "--cli" }
if ($Status) { $args += "--status" }

# Run launcher
if ($args.Count -gt 0) {
    python launcher.py @args
} else {
    python launcher.py
}

# Keep window open if running interactively
if ($Host.Name -eq "ConsoleHost") {
    Write-Host ""
    Write-Host "Press any key to exit..." -ForegroundColor DarkGray
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
