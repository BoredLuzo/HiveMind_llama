@echo off
setlocal
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

where docker >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] docker not found. Install and start Docker Desktop:
    echo          https://www.docker.com/products/docker-desktop/
    echo.
    echo  If you already run SearXNG somewhere else, use:
    echo          searxng.bat external http://HOST:PORT
    echo.
    if "%ACTION%"=="status" exit /b 1
    echo  Press any key to continue... & pause >nul & exit /b 1
)

if /i "%ACTION%"=="install" goto install
if /i "%ACTION%"=="start" goto start
if /i "%ACTION%"=="stop" goto stop
if /i "%ACTION%"=="restart" goto restart
if /i "%ACTION%"=="status" goto status
echo  Unknown action: %ACTION%
echo.
exit /b 1

:external
echo.
set "HM_EXT=%~2"
if "%HM_EXT%"=="" (
    set /p "HM_EXT=SearXNG URL (e.g. http://localhost:8888): "
)
if "%HM_EXT%"=="" (
    echo  [ERROR] No SearXNG URL given.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
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
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo.
echo  --------------------------------------------------------------
echo   [OK] searxng_host set to %HM_EXT%.
echo.
echo   Restart HiveMind, then use the websearch "Check status"
echo   button in the UI to verify the connection.
echo  --------------------------------------------------------------
echo.
echo  Press any key to continue...
pause >nul
exit /b 0

:install
echo.
choice /c YN /n /m "Install and start SearXNG on port %HM_PORT% (new secret + image rebuild)? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was changed.
    echo.
    exit /b 0
)
echo.
echo  Target folder: %~dp0searxng-config
echo  Port:          http://localhost:%HM_PORT%
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
    echo  Press any key to continue... & pause >nul & exit /b 1
)
goto running

:start
echo.
choice /c YN /n /m "Start the SearXNG container now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - container stays stopped.
    echo.
    exit /b 0
)
echo.
docker start hivemind-searxng >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Container does not exist - creating it...
    docker compose -f docker-compose.yml up -d --build --force-recreate --renew-anon-volumes
    if errorlevel 1 (
        echo.
        echo  [ERROR] docker compose failed. Is Docker Desktop running?
        echo.
        echo  Press any key to continue... & pause >nul & exit /b 1
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
    exit /b 0
)
echo.
docker stop hivemind-searxng >nul 2>&1
if errorlevel 1 ( echo  [INFO] Container was not running. ) else ( echo  [OK] SearXNG stopped. )
echo.
exit /b 0

:restart
echo.
choice /c YN /n /m "Restart the SearXNG container now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was restarted.
    echo.
    exit /b 0
)
echo.
docker restart hivemind-searxng >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] docker restart failed - is the container created?
    echo          Run "searxng.bat install" first.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
goto running

:status
docker ps --filter "name=hivemind-searxng" --format "{{.Names}}: {{.Status}}"
if errorlevel 1 exit /b 1
docker inspect -f "{{.State.Running}}" hivemind-searxng >nul 2>&1
if errorlevel 1 ( echo  [INFO] Container not created - run "searxng.bat install" ) & exit /b 0

:running
echo.
echo  --------------------------------------------------------------
echo   [OK] SearXNG is running on http://localhost:%HM_PORT%
echo.
echo   Restart policy: unless-stopped - starts with Docker Desktop.
echo   Port changed? Re-run "searxng.bat install %HM_PORT%" and restart
echo   HiveMind (settings.json searxng_host was updated automatically).
echo  --------------------------------------------------------------
echo.
echo  Press any key to continue...
pause >nul
exit /b 0

