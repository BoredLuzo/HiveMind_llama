@echo off
setlocal
REM WORKDIR-FIX (2026-10-17): always run relative to the install folder, so a
REM double-click, a shortcut or a call from another directory all behave the same.
cd /d "%~dp0"

REM INTERACTIVE (2026-10-17, live report "it just closed itself"): with no
REM arguments the script is almost always double-clicked - the window must stay
REM open at the END of every path, and it must never fail without a message
REM (a dead Docker daemon used to print a raw docker error and exit instantly).
set "ARG1=%~1"
set "INTERACTIVE="
if "%ARG1%"=="" set "INTERACTIVE=1"
set "RC=0"

if not exist "%~dp0searxng-config" (
    echo.
    echo  [ERROR] "searxng-config" is missing next to searxng.bat.
    echo          Restore it from the release zip or the git checkout
    echo          ^(docker-compose.yml.example + settings.yml.example^).
    set "RC=1"
    goto :finish
)

cd /d "%~dp0searxng-config"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - SEARXNG MANAGER                               ^|
echo  ^|    Search engine ^(Docker container^)                         ^|
echo  +=============================================================+
echo.
echo  Usage:  searxng.bat [install^|start^|stop^|restart^|status^|external] [port-or-url]
echo.
echo    install   generate a secret, build the image and start the container
echo    start     start the container (created automatically on first run)
echo    stop      stop the container
echo    restart   restart the container
echo    status    show the current container state
echo    external  point HiveMind at an ALREADY-RUNNING SearXNG instance
echo              (no Docker needed) - e.g. searxng.bat external http://localhost:8888
echo.
echo  Notes:
echo    - The settings.yml is baked into the image (no host bind mounts -
echo      avoids Docker Desktop WSL2 path translation errors).
echo    - The container runs with "restart: unless-stopped" and starts
echo      automatically once Docker Desktop is running.
echo    - The port (default 8888) comes from argument 2, the SEARXNG_PORT
echo      env var, or settings.json. "install" rewrites docker-compose.yml
echo      (host port + SEARXNG_BASE_URL) and updates settings.json.
echo    - "external" only writes settings.json (searxng_host) - it never
echo      touches Docker. Use it for a self-hosted or remote SearXNG.
echo.

REM Find Python (venv preferred, then system) - for settings.json updates
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

REM ---- Port resolution: arg > SEARXNG_PORT env > existing settings.json > 8888 ----
set "HM_PORT=%~2"
if "%HM_PORT%"=="" set "HM_PORT=%SEARXNG_PORT%"
if "%HM_PORT%"=="" (
    if exist "..\settings.json" (
        for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$j = Get-Content '..\settings.json' -Raw | ConvertFrom-Json; if ($j.searxng_host -match 'localhost:(\d+)') { $matches[1] }"`) do set "HM_PORT=%%P"
    )
)
if "%HM_PORT%"=="" set "HM_PORT=8888"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=status"

REM "external" points at an already-running SearXNG - no Docker required.
REM It only writes settings.json (searxng_host). Runs BEFORE the docker check.
if /i "%ACTION%"=="external" goto external

REM DAEMON-CHECK (2026-10-17): "docker" on PATH is not enough - Docker Desktop
REM being stopped is the common case and produced a raw error + instant close.
docker info >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Docker is not reachable - Docker Desktop is not running.
    echo.
    echo          Start Docker Desktop, wait for "Engine running", then run
    echo          this script again.
    echo.
    echo          No Docker on this machine? Point HiveMind at an existing
    echo          SearXNG instead:
    echo              searxng.bat external http://HOST:PORT
    echo.
    set "RC=1"
    goto :finish
)

if /i "%ACTION%"=="install" goto install
if /i "%ACTION%"=="start" goto start
if /i "%ACTION%"=="stop" goto stop
if /i "%ACTION%"=="restart" goto restart
if /i "%ACTION%"=="status" goto status
echo  Unknown action: %ACTION%
echo.
set "RC=1"
goto :finish

:external
echo.
set "HM_EXT=%~2"
if "%HM_EXT%"=="" (
    set /p "HM_EXT=SearXNG URL (e.g. http://localhost:8888): "
)
if "%HM_EXT%"=="" (
    echo  [ERROR] No SearXNG URL given.
    echo.
    set "RC=1"
    goto :finish
)
echo  Pointing HiveMind at: %HM_EXT%
echo.
REM settings.py lives in the repo ROOT - run Python from there.
pushd "%~dp0"
"%PY%" -c "from settings import load_settings, save_settings; s = load_settings(); s['searxng_host'] = '%HM_EXT%'; save_settings(s); print('   searxng_host -> ' + s['searxng_host'])"
popd
if errorlevel 1 (
    echo.
    echo  [ERROR] Could not write settings.json.
    echo.
    set "RC=1"
    goto :finish
)
echo.
echo  --------------------------------------------------------------
echo   [OK] searxng_host set to %HM_EXT%.
echo.
echo   Restart HiveMind, then use the websearch "Check status"
echo   button in the UI to verify the connection.
echo  --------------------------------------------------------------
set "RC=0"
goto :finish

:install
echo.
choice /c YN /n /m "Install and start SearXNG on port %HM_PORT% (new secret + image rebuild)? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was changed.
    echo.
    set "RC=0"
    goto :finish
)
echo.
echo  Target folder: %~dp0searxng-config
echo  Port:          http://localhost:%HM_PORT%
echo.
REM SEED-TEMPLATES (2026-09-03): docker-compose.yml + settings.yml are
REM gitignored (per-machine secret + port). A fresh clone has only the
REM *.example files - seed the real ones from them before rewriting.
if not exist "docker-compose.yml" (
    if exist "docker-compose.yml.example" (
        copy /y "docker-compose.yml.example" "docker-compose.yml" >nul
        echo  [..] docker-compose.yml seeded from template.
    ) else (
        echo  [ERROR] docker-compose.yml.example missing - re-clone or restore it.
        set "RC=1"
        goto :finish
    )
)
if not exist "settings.yml" (
    if exist "settings.yml.example" (
        copy /y "settings.yml.example" "settings.yml" >nul
        echo  [..] settings.yml seeded from template.
    ) else (
        echo  [ERROR] settings.yml.example missing - re-clone or restore it.
        set "RC=1"
        goto :finish
    )
)
echo.
echo  [..] Generating secrets (compose + settings)...
powershell -NoProfile -Command "$hex = -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) }); foreach ($f in @('docker-compose.yml','settings.yml')) { $c=[System.IO.File]::ReadAllText($f); $c=$c -replace 'SEARXNG_SECRET=.*', ('SEARXNG_SECRET=' + $hex) -replace 'secret_key: \".*\"', ('secret_key: \"' + $hex + '\"'); [System.IO.File]::WriteAllText($f, $c) }"
REM Adapt host port + SEARXNG_BASE_URL in docker-compose.yml
powershell -NoProfile -Command "$p='%HM_PORT%'; $f='docker-compose.yml'; $c=[System.IO.File]::ReadAllText($f); $c=$c -replace '127\.0\.0\.1:\d+:8080', ('127.0.0.1:' + $p + ':8080') -replace 'SEARXNG_BASE_URL=http://localhost:\d+', ('SEARXNG_BASE_URL=http://localhost:' + $p); [System.IO.File]::WriteAllText($f, $c)"
REM Point settings.json searxng_host at the new port. settings.py lives in the
REM repo ROOT, so run Python from there (this script cd's into searxng-config).
pushd "%~dp0"
"%PY%" -c "from settings import load_settings, save_settings; s = load_settings(); s['searxng_host'] = 'http://localhost:%HM_PORT%'; save_settings(s); print('   searxng_host -> ' + s['searxng_host'])"
popd
echo.
echo  [..] Building image + starting container (port %HM_PORT%)...
docker compose -f docker-compose.yml up -d --build --force-recreate --renew-anon-volumes
if errorlevel 1 (
    echo.
    echo  [ERROR] docker compose failed. Is Docker Desktop running?
    echo.
    set "RC=1"
    goto :finish
)
goto running

:start
echo.
choice /c YN /n /m "Start the SearXNG container now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - container stays stopped.
    echo.
    set "RC=0"
    goto :finish
)
echo.
REM Seed gitignored runtime files from *.example templates if absent.
if not exist "docker-compose.yml" (
    if exist "docker-compose.yml.example" copy /y "docker-compose.yml.example" "docker-compose.yml" >nul
)
if not exist "settings.yml" (
    if exist "settings.yml.example" copy /y "settings.yml.example" "settings.yml" >nul
)
docker start hivemind-searxng >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Container does not exist - creating it...
    docker compose -f docker-compose.yml up -d --build --force-recreate --renew-anon-volumes
    if errorlevel 1 (
        echo.
        echo  [ERROR] docker compose failed. Is Docker Desktop running?
        echo.
        set "RC=1"
        goto :finish
    )
)
goto running

:stop
echo.
choice /c YN /n /m "Stop the SearXNG container now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - container keeps running.
    echo.
    set "RC=0"
    goto :finish
)
echo.
docker stop hivemind-searxng >nul 2>&1
if errorlevel 1 ( echo  [INFO] Container was not running. ) else ( echo  [OK] SearXNG stopped. )
echo.
set "RC=0"
goto :finish

:restart
echo.
choice /c YN /n /m "Restart the SearXNG container now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was restarted.
    echo.
    set "RC=0"
    goto :finish
)
echo.
docker restart hivemind-searxng >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] docker restart failed - is the container created?
    echo          Run "searxng.bat install" first.
    echo.
    set "RC=1"
    goto :finish
)
goto running

:status
REM STATUS-TRUTH (2026-10-17): the old path printed the "[OK] running" box for a
REM STOPPED container and exited silently when the daemon was down.
docker ps -a --filter "name=hivemind-searxng" --format "{{.Names}}: {{.Status}}"
docker inspect -f "{{.State.Running}}" hivemind-searxng >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [INFO] Container not created yet - run "searxng.bat install"
    set "RC=0"
    goto :finish
)
set "RUNNING="
for /f "usebackq delims=" %%R in (`docker inspect -f "{{.State.Running}}" hivemind-searxng`) do set "RUNNING=%%R"
if /i "%RUNNING%"=="true" goto running
echo.
echo  [INFO] Container exists but is STOPPED - run "searxng.bat start".
set "RC=0"
goto :finish

:running
echo.
echo  --------------------------------------------------------------
echo   [OK] SearXNG is running on http://localhost:%HM_PORT%
echo.
echo   Restart policy: unless-stopped - starts with Docker Desktop.
echo   Port changed? Re-run "searxng.bat install %HM_PORT%" and restart
echo   HiveMind (settings.json searxng_host was updated automatically).
echo  --------------------------------------------------------------
set "RC=0"
goto :finish

:finish
REM Keep the window open when the script was double-clicked (no arguments);
REM a call from an open terminal keeps its normal exit code and returns.
if defined INTERACTIVE (
    echo.
    echo  Press any key to close this window...
    pause >nul
)
exit /b %RC%
