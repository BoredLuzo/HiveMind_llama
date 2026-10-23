@echo off
REM ENGINE WATCHDOG (2026-10-22): the live engine died repeatedly within
REM minutes of a start - no crash in the event log, no OOM (20 GB free),
REM no ATEXIT -> it was terminated externally. This watchdog restarts the
REM engine (headless, logged) whenever port 8001 stops listening.
REM
REM HEARTBEAT MARKER + STOP MODE (2026-10-23): the loop rewrites the marker
REM file on every pass. A marker younger than 90 s means another watchdog is
REM on duty and a second instance exits; an older marker is a crashed
REM leftover and is taken over. "watchdog_engine.bat stop" removes the
REM marker and the loop exits within one pass. start_hivemind.bat calls
REM stop before a manual start: the 20 s loop window used to re-grab port
REM 8001 right after the kill and the fresh foreground instance crashed
REM with winerror 10048 (live, 2026-10-23).
REM Linear flow, no parenthesized blocks: the powershell expressions carry
REM parentheses that terminate a batch if-block (live parse error).
setlocal EnableExtensions
cd /d "%~dp0"
set "PORT=8001"
set "WDLOG=%LOCALAPPDATA%\HiveMindGateway\watchdog.log"
set "WDMARK=%LOCALAPPDATA%\HiveMindGateway\watchdog.marker"

if /i not "%~1"=="stop" goto maybe_takeover
if exist "%WDMARK%" del "%WDMARK%" >nul 2>&1
echo [%date% %time%] watchdog stop requested - marker removed >> "%WDLOG%"
exit /b 0

:maybe_takeover
if not exist "%WDMARK%" goto duty
powershell -NoProfile -Command "if (((Get-Date) - (Get-Item '%WDMARK%').LastWriteTime).TotalSeconds -lt 90) { exit 1 }" >nul 2>&1
if errorlevel 1 goto already_running
goto duty

:already_running
echo [%date% %time%] watchdog already on duty (fresh marker) - exiting >> "%WDLOG%"
exit /b 0

:duty
echo watchdog on duty [%date% %time%] > "%WDMARK%"
echo [%date% %time%] watchdog started >> "%WDLOG%"

:loop
if not exist "%WDMARK%" goto stopped
echo [%date% %time%] > "%WDMARK%"
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>&1
if errorlevel 1 goto wait
echo [%date% %time%] engine down on %PORT% - restarting (headless) >> "%WDLOG%"
call start_engine_headless.bat
echo [%date% %time%] engine exited again - watchdog stays on duty >> "%WDLOG%"

:wait
timeout /t 20 /nobreak >nul
goto loop

:stopped
echo [%date% %time%] watchdog stop requested (marker gone) - exiting >> "%WDLOG%"
exit /b 0
