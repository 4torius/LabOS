@echo off
REM OpentronsSiLA2Server - Start Client
REM ===================================
cd /d "%~dp0"
echo.
echo ========================================
echo   OPENTRONS SiLA2 CLIENT
echo ========================================
echo.

REM Activate virtual environment if exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Start client
python ../opentrons_client.py %*

pause
