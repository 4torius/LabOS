@echo off
REM OpentronsSiLA2Server - Connection Test
REM ======================================

echo.
echo ========================================
echo   OPENTRONS CONNECTION TEST
echo ========================================
echo.

REM Activate virtual environment if exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Run test
python main.py --test

pause
