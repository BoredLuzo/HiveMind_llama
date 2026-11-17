@echo off
setlocal EnableExtensions
REM Headless gateway start for autostart/detached use (Task Scheduler,
REM WMI, remote starts, start_hivemind.bat auto-start). No window, no
REM pause - logs go to the file.
cd /d "%~dp0"
REM I3 (audit r2): cmd cannot redirect into a missing directory -
REM on a gateway-less install the engine autostart silently never ran.
if not exist "%LOCALAPPDATA%\HiveMindGateway" mkdir "%LOCALAPPDATA%\HiveMindGateway"
set "GW_LOG=%LOCALAPPDATA%\HiveMindGateway\gateway_headless.log"
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
    py -3.14 --version >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%X in ('py -3.14 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%X"
    )
)
if not defined PY if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [%date% %time%] [ERROR] No Python interpreter found >> "%GW_LOG%"
    exit /b 1
)
"%PY%" -m hivemind_gateway.main >> "%GW_LOG%" 2>&1
