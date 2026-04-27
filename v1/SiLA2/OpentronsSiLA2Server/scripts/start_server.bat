@echo off
REM OpentronsSiLA2Server - Start Server
REM ====================================

echo.
echo ========================================
echo   OPENTRONS SiLA2 SERVER
echo ========================================
echo.

REM Activate virtual environment if exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
    echo Virtual environment activated
)

REM Start server
python ../main.py %*

pause
