@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - RELEASE CLEANUP                               ^|
echo  ^|    Removes caches, runtime data and generated files         ^|
echo  +=============================================================+
echo.
echo  This removes:
echo    - __pycache__ / .pytest_cache / .ruff_cache
echo    - runtime_models.json, token_stats.json, run_counter.json
echo    - models.json, memory.json, soul.json, last_workspace.json
echo    - logs\, sessions\, infra\sessions\, learning_logs\,
echo      context\projects\, custom_prompts\
echo    - keeps model_configs\models\ (shipped per-model specs stay intact)
echo    - resets SearXNG secrets to placeholders (CHANGE_ME_42f0b9c1)
echo.
echo  WARNING: Your generated configs and memory are deleted.
echo  Only run this when you really want a clean state.
echo.

choice /c YN /n /m "Run release cleanup now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was changed.
    echo.
    exit /b 0
)
echo.

REM ----------------------------------------------------------
REM [1/4] Python caches
REM ----------------------------------------------------------
echo  ==========================================================
echo   [1/4] Python caches
echo  ==========================================================
for /d /r "%~dp0" %%D in (__pycache__) do (
    rd /s /q "%%D" 2>nul
)
if exist ".pytest_cache" rd /s /q ".pytest_cache"
if exist ".ruff_cache" rd /s /q ".ruff_cache"
echo   [OK] Python caches removed.
echo.

REM ----------------------------------------------------------
REM [2/4] Runtime / generated files (same set as .gitignore)
REM ----------------------------------------------------------
echo  ==========================================================
echo   [2/4] Runtime data and generated files
echo  ==========================================================
for %%F in (
    "runtime_models.json"
    "token_stats.json"
    "run_counter.json"
    "models.json"
    "memory.json"
    "soul.json"
    "context\last_workspace.json"
) do (
    if exist "%%F" del /f /q "%%F"
)
for %%D in (
    "logs"
    "sessions"
    "infra\sessions"
    "learning_logs"
    "model_configs\learning_logs"
    "model_configs\learned"
    "context\projects"
    "custom_prompts"
) do (
    if exist "%%D" rd /s /q "%%D"
)
echo   [OK] Runtime data and generated files removed.
echo.

REM ----------------------------------------------------------
REM [3/4] SearXNG secret reset to placeholders
REM ----------------------------------------------------------
echo  ==========================================================
echo   [3/4] SearXNG secrets reset
echo  ==========================================================
set "PLACEHOLDER=CHANGE_ME_42f0b9c1"
if exist "searxng-config\docker-compose.yml" (
    powershell -NoProfile -Command "$f='searxng-config\docker-compose.yml'; $c=[System.IO.File]::ReadAllText($f); $c=$c -replace 'SEARXNG_SECRET=.*', 'SEARXNG_SECRET=%PLACEHOLDER%' -replace '127\.0\.0\.1:\d+:8080', '127.0.0.1:8888:8080' -replace 'SEARXNG_BASE_URL=http://localhost:\d+', 'SEARXNG_BASE_URL=http://localhost:8888'; [System.IO.File]::WriteAllText($f, $c)"
)
if exist "searxng-config\settings.yml" (
    powershell -NoProfile -Command "$f='searxng-config\settings.yml'; $c=[System.IO.File]::ReadAllText($f); $c=$c -replace 'secret_key: \".*\"', ('secret_key: \"' + '%PLACEHOLDER%' + '\"'); [System.IO.File]::WriteAllText($f, $c)"
)
if exist "settings.json" (
    powershell -NoProfile -Command "$j = Get-Content 'settings.json' -Raw | ConvertFrom-Json; if ($j.searxng_host -and $j.searxng_host -ne 'http://localhost:8888') { $j.searxng_host = 'http://localhost:8888'; $j | ConvertTo-Json -Depth 20 | Set-Content 'settings.json' -Encoding UTF8 }"
)
echo   [OK] SearXNG secrets reset to placeholders.
echo.

REM ----------------------------------------------------------
REM [4/4] Verification
REM ----------------------------------------------------------
echo  ==========================================================
echo   [4/4] Verification
echo  ==========================================================
set "PYC_COUNT=0"
for /r "%~dp0" %%F in (*.pyc) do set /a PYC_COUNT+=1
set "LOG_COUNT=0"
for /r "%~dp0" %%F in (*.log) do set /a LOG_COUNT+=1

echo.
echo  +=============================================================+
if not "%PYC_COUNT%"=="0" (
    echo  ^|  WARNING: %PYC_COUNT% .pyc files remain                   ^|
)
if not "%LOG_COUNT%"=="0" (
    echo  ^|  WARNING: %LOG_COUNT% .log files remain                    ^|
)
if "%PYC_COUNT%"=="0" if "%LOG_COUNT%"=="0" (
    echo  ^|  Release is clean - can be packaged.                      ^|
)
echo  +=============================================================+
echo.
echo  Press any key to continue...
pause >nul

