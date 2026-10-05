@echo off
REM start_gateway.bat - double-click launcher for the Telegram gateway.
REM
REM One-time setup (docs/gateway_setup.md): store the bot token ONCE in the
REM Windows Credential Manager - toggling the gateway on/off afterwards
REM never asks for it again:
REM   start_gateway.bat setup
REM Alternative: set HIVEMIND_TG_TOKEN in this shell before starting.
REM
REM The token is NEVER read from a file and never printed. Owner binding,
REM Telegram offset and the kill switch (gateway.disabled) live in
REM %LOCALAPPDATA%\HiveMindGateway and survive restarts and updates.
cd /d "%~dp0"

REM Python resolution mirrors start_hivemind.bat: the project .venv first
REM (it has httpx; a bare system python may not), then python from PATH.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if "%PY%"=="python" (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] python not found in PATH - install Python 3.10+ first.
        pause
        exit /b 1
    )
)

if /I "%~1"=="setup" (
    echo [..] Token setup: validate + store in the Windows Credential Manager ...
    "%PY%" -m hivemind_gateway.main setup-token
    pause
    exit /b 0
)
echo [..] Starting Telegram gateway (Ctrl+C to stop) ...
"%PY%" -m hivemind_gateway.main
set "GW_EXIT=%ERRORLEVEL%"
echo.
if not "%GW_EXIT%"=="0" (
    echo [INFO] Gateway stopped with exit code %GW_EXIT%. Read the
    echo        messages above - a pairing code window also goes here.
)
echo [INFO] Gateway stopped. This window can be closed.
pause
