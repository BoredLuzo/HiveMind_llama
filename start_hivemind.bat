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
    echo    by: Luzo  ^|  v1.1.3
echo.

REM Resolve server port: settings.json "server_port" (set by install.bat), default 8001.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$j = Get-Content 'settings.json' -Raw -ErrorAction SilentlyContinue | ConvertFrom-Json; if ($j.server_port) { $j.server_port } else { '8001' }"`) do set HM_PORT=%%P
if not defined HM_PORT set "HM_PORT=8001"

REM Port check: is HiveMind already running?
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %HM_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($c) { $c }"`) do set HM_PORT_PID=%%P
if not defined HM_PORT_PID goto port_check_done
echo.
echo  [INFO] HiveMind is already running on port %HM_PORT% (PID: %HM_PORT_PID%).
echo  [INFO] Open: http://localhost:%HM_PORT%
echo.
choice /c YN /n /m "Kill the running instance and start fresh? [Y/N] "
if errorlevel 2 (
    echo  Aborted - existing instance stays running.
    exit /b 0
)
# SERIAL-RESTART (2026-09-10): kill the old instance with its process tree
# (llama-server children) - no labels inside parenthesized blocks: cmd's
# parser breaks on them.
echo  [..] Killing PID %HM_PORT_PID% ...
taskkill /F /T /PID %HM_PORT_PID%
powershell -NoProfile -Command "Stop-Process -Id %HM_PORT_PID% -Force -ErrorAction SilentlyContinue"
# KILL-WAIT: process death + socket release can take several seconds - poll.
set /a HM_KILL_WAIT=0
:kill_wait_loop
timeout /t 1 /nobreak >nul
set "HM_STILL="
for /f "usebackq delims=" %%Q in (`powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %HM_PORT% -State Listen -ErrorAction SilentlyContinue) { '1' }"`) do set HM_STILL=%%Q
if not defined HM_STILL goto port_check_done
set /a HM_KILL_WAIT+=1
if %HM_KILL_WAIT% lss 10 goto kill_wait_loop
:port_check_done
)
:port_check_done
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %HM_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($c) { $c }"`) do set HM_PORT_PID=%%P
if defined HM_PORT_PID (
    echo  [WARN] Port %HM_PORT% still occupied ^(PID: %HM_PORT_PID%^) - cannot start.
    echo  Press any key to continue...
    pause >nul
    exit /b 1
)
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

REM Quick dependency check (installs missing packages on demand).
REM UV-CONSISTENCY (2026-09-09): uv-managed .venvs ship WITHOUT pip, so the
REM old `python -m pip` fallback was dead exactly when it was needed. Prefer
REM uv, fall back to pip only for legacy system-python setups.
"%PY%" -c "import httpx, fastapi, uvicorn, rich" >nul 2>&1
if not errorlevel 1 goto deps_ok
echo  [..] Installing missing packages...
where uv >nul 2>&1
if not errorlevel 1 goto deps_uv
"%PY%" -m pip install httpx fastapi uvicorn rich watchfiles --quiet
goto deps_done
:deps_uv
uv pip install --python "%PY%" httpx fastapi uvicorn rich watchfiles --quiet
:deps_done
if errorlevel 1 (
    echo  [WARN] Package install failed - the server may not start.
) else (
    echo  [OK] Packages installed.
)
echo.
:deps_ok

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

