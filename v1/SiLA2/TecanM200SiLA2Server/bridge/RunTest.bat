@echo off
echo ===========================================
echo   Tecan SiLA2 - Test Procedure
echo ===========================================
echo.

set SERVER_PORT=50055
set ROOT=%~dp0

echo Starting server on port %SERVER_PORT%...
start "TecanServer" /MIN "%ROOT%bin\Debug\net48\TecanSiLA2Server.exe" %SERVER_PORT%

echo Waiting for server to start...
timeout /t 3 /nobreak > nul

echo.
echo Starting TestClient...
echo.
"%ROOT%TestClient\bin\Debug\net48\TestClient.exe" localhost %SERVER_PORT%

echo.
echo Stopping server...
taskkill /FI "WINDOWTITLE eq TecanServer" /T /F > nul 2>&1

echo Done.
pause
