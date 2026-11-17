@echo off
setlocal

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - WINDOWS SERVICE                               ^|
echo  ^|    Install HiveMind as a Windows service ^(NSSM^)             ^|
echo  +=============================================================+
echo.
echo  What this does:
echo    - Checks for Administrator rights (required for services).
echo    - Checks for NSSM (in PATH or the deploy directory).
echo  - Installs HiveMind as the "HiveMind" Windows service via NSSM.
echo.
echo  Start service:  sc start HiveMind
echo  Stop service:   sc stop HiveMind
echo  View logs:      type "%PROGRAMDATA%\HiveMind\Logs\hivemind.log"
echo.

:: Check admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Run this script as Administrator.
    echo  Right-click ^> Run as administrator
    echo.
    echo  Press any key to continue...
    pause >nul
    exit /b 1
)

:: Check NSSM
set "NSSM_BIN=nssm"
where nssm >nul 2>&1
if %errorlevel% neq 0 (
    if exist "%~dp0nssm.exe" (
        set "NSSM_BIN=%~dp0nssm.exe"
        echo  [OK] Found nssm.exe in the deploy directory.
    ) else (
        echo  [ERROR] nssm.exe not found in PATH or the deploy directory.
        echo.
        echo  Download NSSM from: https://nssm.cc/download
        echo  Extract and place nssm.exe in: %~dp0
        echo  Then re-run this script.
        echo.
        echo  Press any key to continue...
        pause >nul
        exit /b 1
    )
)

:: Check Python - the install venv FIRST (uv-managed installs have no python
:: on PATH, and NSSM needs the full interpreter path for the LocalSystem service),
:: then whatever python is on PATH.
set "HMPY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "HMPY=%~dp0..\.venv\Scripts\python.exe"
if defined HMPY goto python_ok
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] python not found - run install.bat first ^(it builds .venv^)
    echo  or put a Python on PATH.
    echo.
    echo  Press any key to continue...
    pause >nul
    exit /b 1
)
set "HMPY=python"
echo  [WARN] .venv not found - using PATH python ^(LocalSystem service may not see user PATH; run install.bat first^).
:python_ok
echo  [OK] Python: %HMPY%
echo  [OK] All prerequisites found.
echo.
echo  App entry:       %~dp0..\run.py
echo  Log folder:      %PROGRAMDATA%\HiveMind\Logs\hivemind.log
echo.

choice /c YN /n /m "Install the HiveMind Windows service now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - service was not installed.
    echo.
    exit /b 0
)
echo.

if not exist "%PROGRAMDATA%\HiveMind\Logs" mkdir "%PROGRAMDATA%\HiveMind\Logs" >nul 2>&1

echo  [..] Installing HiveMind service...
set "NSSM_FAIL=0"
"%NSSM_BIN%" install HiveMind "%HMPY%" "\"%~dp0..\run.py\""
if errorlevel 1 set "NSSM_FAIL=1"
if "%NSSM_FAIL%"=="0" (
    "%NSSM_BIN%" set HiveMind AppDirectory "%~dp0.."
    if errorlevel 1 set "NSSM_FAIL=1"
    "%NSSM_BIN%" set HiveMind AppStdout "%PROGRAMDATA%\HiveMind\Logs\hivemind.log"
    if errorlevel 1 set "NSSM_FAIL=1"
    "%NSSM_BIN%" set HiveMind AppStderr "%PROGRAMDATA%\HiveMind\Logs\hivemind.log"
    if errorlevel 1 set "NSSM_FAIL=1"
)

if "%NSSM_FAIL%"=="0" (
    echo.
    echo  === Installation complete ===
    echo.
    echo  Start the service:
    echo    sc start HiveMind
    echo.
    echo  Stop the service:
    echo    sc stop HiveMind
    echo.
    echo  View logs:
    echo    type "%PROGRAMDATA%\HiveMind\Logs\hivemind.log"
    echo.
) else (
    echo.
    echo  Installation failed. Check errors above.
    echo.
)

echo  Press any key to continue...

pause >nul