@echo off
REM Headless gateway start for autostart/detached use (Task Scheduler,
REM WMI, remote starts, start_hivemind.bat auto-start). No window, no
REM pause - logs go to the file.
cd /d "%~dp0"
REM I3 (audit r2): cmd cannot redirect into a missing directory -
REM on a gateway-less install the engine autostart silently never ran.
if not exist "%LOCALAPPDATA%\HiveMindGateway" mkdir "%LOCALAPPDATA%\HiveMindGateway"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m hivemind_gateway.main >> "%LOCALAPPDATA%\HiveMindGateway\gateway_headless.log" 2>&1
) else (
    python -m hivemind_gateway.main >> "%LOCALAPPDATA%\HiveMindGateway\gateway_headless.log" 2>&1
)
