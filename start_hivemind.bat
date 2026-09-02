@echo off
cd /d "%~dp0"

echo.
echo       ___
echo    __/   \__
echo   /  \___/  \
echo   \__/   \__/
echo   /  \___/  \
echo   \__/   \__/
echo      \___/
echo.
echo    H I V E M I N D
echo    by: Luzo  ^|  v1.0.5
echo.

REM Resolve server port: settings.json "server_port" (set by install.bat), default 8001.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$j = Get-Content 'settings.json' -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; if ($j.server_port) { $j.server_port } else { '8001' }"`) do set HM_PORT=%%P
if not defined HM_PORT set "HM_PORT=8001"

REM Port check: is HiveMind already running?
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %HM_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($c) { $c }"`) do set HM_PORT_PID=%%P
if defined HM_PORT_PID (
    echo.
    echo  [INFO] HiveMind is already running on port %HM_PORT% ^(PID: %HM_PORT_PID%^).
    echo  [INFO] Open: http://localhost:%HM_PORT%
    echo.
    echo  Press any key to continue...
    pause >nul
    exit /b 0
)

REM Find Python: venv first, then py -3.14 (resolved to a real path),
REM then known install locations. "%PY%" is always quote-safe this way.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
) else (
    py -3.14 --version >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%X in ('py -3.14 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%X"
    )
    if "%PY%"=="python" if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
    if "%PY%"=="python" if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
)

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found. Please run install.bat first.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo  [OK] Python: %PY%
echo.

REM Quick dependency check (installs missing packages on demand)
"%PY%" -c "import httpx, fastapi, uvicorn, rich" >nul 2>&1
if errorlevel 1 (
    echo  [..] Installing missing packages...
    "%PY%" -m pip install httpx fastapi uvicorn rich watchfiles --quiet
    echo  [OK] Packages installed.
    echo.
)

REM Hints when llama.cpp / models are missing:
if not exist "llama" (
    echo  [NOTE] Folder 'llama\' is missing - run install.bat first or
    echo         extract llama.cpp manually into it.
    echo.
)
REM Models available via: env HIVEMIND_MODELS_DIR > settings.json "models_dir"
REM > repo\models\ > models.json (absolute GGUF paths from setup_models.bat).
for /f "usebackq delims=" %%M in (`powershell -NoProfile -Command "$ok=$false; if ($env:HIVEMIND_MODELS_DIR) { $ok=$true }; if (Test-Path 'models') { $ok=$true }; try { $s=Get-Content 'settings.json' -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; if ($s.models_dir) { $ok=$true } } catch {}; try { $j=Get-Content 'models.json' -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; $n=0; foreach($p in $j.PSObject.Properties){ if(-not $p.Name.StartsWith('_') -and $p.Value) { $n++ } }; if ($n -gt 0) { $ok=$true } } catch {}; if ($ok) { '1' } else { '0' }"`) do set HM_HAS_MODELS=%%M
if not "%HM_HAS_MODELS%"=="1" (
    echo  [NOTE] No models configured. Run setup_models.bat, set
    echo         HIVEMIND_MODELS_DIR, or drop GGUFs into the models\ folder.
    echo.
)

echo  --------------------------------------------------------------
echo   HiveMind  ^>  http://localhost:%HM_PORT%   ^|   Ctrl+C to stop
echo  --------------------------------------------------------------
echo.
"%PY%" run.py
if errorlevel 1 (
    echo.
    echo  [ERROR] HiveMind crashed. Read the error message above.
    echo.
    echo  Press any key to continue...
    pause >nul
)

