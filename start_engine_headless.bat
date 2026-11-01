@echo off
setlocal
REM Headless engine start for autostart/detached use (Task Scheduler,
REM WMI, remote starts). No window, no pause - logs go to the file.
cd /d "%~dp0"
set "HMLOG=%LOCALAPPDATA%\HiveMindGateway\engine_headless.log"
REM I3 (audit r2): cmd cannot redirect into a missing directory -
REM on a gateway-less install the engine autostart silently never ran.
if not exist "%LOCALAPPDATA%\HiveMindGateway" mkdir "%LOCALAPPDATA%\HiveMindGateway"

REM LAUNCH-FORENSICS (2026-11-01, live 20:11): the engine vanished and the
REM watchdog restarted it every 20 s for 20 minutes without a single line in
REM the log - the python never wrote anything, so the LAUNCH failed. Record
REM the launcher's own steps and the exit code, or the next silent death is
REM again unexplainable.
set "HMPY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "HMPY=%~dp0.venv\Scripts\python.exe"
echo [%date% %time%] launcher start (python=%HMPY%) >> "%HMLOG%" 2>&1
if not exist "%~dp0run.py" (
    echo [%date% %time%] [FATAL] run.py missing in %~dp0 >> "%HMLOG%" 2>&1
    exit /b 2
)
"%HMPY%" run.py >> "%HMLOG%" 2>&1
set "HMRC=%ERRORLEVEL%"
echo [%date% %time%] engine exited with code %HMRC% >> "%HMLOG%" 2>&1
if not "%HMRC%"=="0" (
    echo [%date% %time%] [NOTE] non-zero exit - a traceback should be above this line >> "%HMLOG%" 2>&1
)
exit /b %HMRC%
