@echo off
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - STOP SERVER ^& LLAMA                           ^|
echo  ^|    Stops HiveMind and frees VRAM                            ^|
echo  +=============================================================+
echo.
echo  What this does:
echo    1) Stops the HiveMind server (the Python process listening on
echo       the configured port).
echo    2) Stops ALL llama-server.exe processes (frees up VRAM).
echo.

choice /c YN /n /m "Stop HiveMind and all llama.cpp processes now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was stopped.
    echo.
    exit /b 0
)
echo.

REM Resolve server port: settings.json "server_port" (set by install.bat), default 8001.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$j = Get-Content 'settings.json' -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; if ($j.server_port) { $j.server_port } else { '8001' }"`) do set HM_PORT=%%P
if not defined HM_PORT set "HM_PORT=8001"

set "SRV_STOPPED="
for /f "usebackq delims=" %%X in (`powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %HM_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($c) { Stop-Process -Id $c -Force -ErrorAction SilentlyContinue; Write-Output 'yes' }"`) do set "SRV_STOPPED=1"
if defined SRV_STOPPED (
    echo  [OK] HiveMind server stopped.
) else (
    echo  [INFO] HiveMind server was not running.
)
echo.

taskkill /F /IM llama-server.exe /T 2>nul
if errorlevel 1 (
    echo  [INFO] No llama-server process was running.
) else (
    echo  [OK] All llama-server processes stopped.
)

echo.
echo  Done. VRAM has been released.
echo.
echo  Press any key to continue...
pause >nul