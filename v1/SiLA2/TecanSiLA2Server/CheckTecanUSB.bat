@echo off
REM ============================================================
REM  Diagnostica Connessione Tecan M200 Pro
REM ============================================================
echo.
echo ============================================================
echo   DIAGNOSTICA CONNESSIONE TECAN M200 PRO
echo ============================================================
echo.

echo [1] Verifico dispositivi USB Tecan...
echo.
powershell -Command "Get-PnpDevice | Where-Object { $_.FriendlyName -like '*Tecan*' -or $_.FriendlyName -like '*Reader*' -or $_.FriendlyName -like '*iControl*' } | Format-Table Status, Class, FriendlyName -AutoSize"
echo.

echo [2] Verifico porte COM virtuali...
echo.
powershell -Command "Get-PnpDevice -Class Ports | Where-Object { $_.Status -eq 'OK' } | Format-Table FriendlyName -AutoSize"
echo.

echo [3] Verifico servizi Tecan...
echo.
powershell -Command "Get-Service | Where-Object { $_.Name -like '*Tecan*' -or $_.DisplayName -like '*Tecan*' } | Format-Table Status, Name, DisplayName -AutoSize"
echo.

echo [4] Verifico processi iControl in esecuzione...
echo.
powershell -Command "Get-Process | Where-Object { $_.ProcessName -like '*iControl*' -or $_.ProcessName -like '*Tecan*' } | Format-Table Id, ProcessName -AutoSize"
echo.

echo ============================================================
echo   SUGGERIMENTI
echo ============================================================
echo.
echo Se non vedi dispositivi Tecan USB:
echo   1. Verifica che il Tecan M200 Pro sia ACCESO
echo   2. Verifica che il cavo USB sia collegato correttamente
echo   3. Prova a riavviare il Tecan M200
echo   4. Reinstalla i driver da: C:\Program Files\Tecan\iControl\Drivers
echo.
echo Se vedi il dispositivo ma la connessione non funziona:
echo   1. Chiudi tutte le istanze di iControl
echo   2. Riavvia il servizio Tecan (se presente)
echo   3. Prova a riavviare il PC
echo.
pause
