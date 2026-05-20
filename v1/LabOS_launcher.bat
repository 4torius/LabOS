@echo off
title LabOS Launcher
cd /d "%~dp0"

REM ═══════════════════════════════════════════════════════════════
REM                       LabOS LAUNCHER
REM        Double-click this file to start the system!
REM ═══════════════════════════════════════════════════════════════

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Python not found!
    echo Please install Python 3.10+ and add it to PATH
    echo.
    pause
    exit /b 1
)

REM Check for virtual environment
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM Run the launcher
python launcher.py
if errorlevel 1 pause
