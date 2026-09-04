@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - INSTALLATION ^(WINDOWS^)                        ^|
echo  ^|    Full setup: Python, packages, GPU backend                ^|
echo  +=============================================================+
echo.
echo  What this does (you will be asked before every step):
echo    [1/6] Python            - set up python environment
echo    [2/6] GPU backend       - CUDA (NVIDIA) or Vulkan (AMD/Intel)
echo    [3/6] llama.cpp backend - downloaded for the chosen backend
echo    [4/6] Models            - downloaded into \models
echo    [5/6] SearXNG           - optional web search (requires Docker)
echo    [6/6] Desktop shortcut  - optional, with the HiveMind icon
echo.

choice /c YN /n /m "Install HiveMind now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was installed.
    echo.
    exit /b 0
)
echo.

REM ======================================================
REM [1/6] Python
REM ======================================================
echo  ==========================================================
echo   [1/6] Setting up Python
echo  ==========================================================
echo.

REM ======================================================
REM uv (fast Python package manager)
REM ======================================================
echo  ==========================================================
echo   [uv] Setting up package manager
echo  ==========================================================
echo.
where uv >nul 2>&1
if errorlevel 1 (
    echo  [uv] uv not found - installing...
    echo  [uv] Target: %USERPROFILE%\.local\bin (added to PATH)
    where winget >nul 2>&1
    if not errorlevel 1 (
        REM --source winget supresses the MS store license agreement prompt
        winget install --id astral-sh.uv --source winget --silent
        set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
    ) else (
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        set "PATH=%PATH%;%USERPROFILE%\.local\bin"
    )
    where uv >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   [ERROR] uv could not be installed.
        echo   Install it manually: https://docs.astral.sh/uv/
        echo.
        echo  Press any key to continue... & pause >nul & exit /b 1
    )
)
uv --version

SET "UV_NO_DEV=1"
echo.

REM ======================================================
REM Virtual environment (keeps the system Python clean)
REM ======================================================
echo  ==========================================================
echo   [venv] Setting up virtual environment
echo  ==========================================================
echo.
uv sync --all-extras

REM ======================================================
REM [playwright] Chromium browser (for browser_tool)
REM ======================================================
echo  ==========================================================
echo   [playwright] Installing Chromium browser
echo  ==========================================================
echo.
uv run playwright install chromium
if errorlevel 1 (
    echo.
    echo   [ERROR] Chromium installation failed.
    echo   Browser tool will not work without it.
    echo.
) else (
    echo   OK.
)
echo.

for /f "delims=" %%X in ('uv python find') do set "PY=%%X"

REM ======================================================
REM [2/6] GPU backend (auto-detection)
REM ======================================================
echo  ==========================================================
echo   [2/6] GPU backend
echo  ==========================================================
echo.
echo   [V] Vulkan  - AMD / Intel GPUs
echo   [C] CUDA    - NVIDIA GPUs
echo.

REM Detect an NVIDIA GPU via nvidia-smi (name + driver CUDA version),
REM fall back to WMI (Win32_VideoAdapter) when nvidia-smi is unavailable.
set "HM_NVIDIA="
set "HM_GPU_NAME="
set "HM_DRIVER_CUDA="
nvidia-smi --query-gpu=name --format=csv,noheader >nul 2>&1
if not errorlevel 1 (
    for /f "usebackq delims=" %%G in (`nvidia-smi --query-gpu=name --format=csv,noheader 2^>nul`) do if not defined HM_GPU_NAME set "HM_GPU_NAME=%%G"
    if defined HM_GPU_NAME (
        set "HM_NVIDIA=1"
        for /f "usebackq delims=" %%C in (`powershell -NoProfile -Command "& nvidia-smi 2>$null | ForEach-Object { if ($_ -match 'CUDA Version:\s*([\d.]+)') { $matches[1] } }"`) do set "HM_DRIVER_CUDA=%%C"
    )
)
if not defined HM_NVIDIA (
    for /f "usebackq delims=" %%G in (`powershell -NoProfile -Command "(Get-CimInstance Win32_VideoAdapter | Where-Object { $_.Name -match 'NVIDIA' } | Select-Object -First 1 -ExpandProperty Name)" 2^>nul`) do if not defined HM_GPU_NAME set "HM_GPU_NAME=%%G"
    if defined HM_GPU_NAME set "HM_NVIDIA=1"
)

if defined HM_NVIDIA (
    echo   Detected: !HM_GPU_NAME!
    if defined HM_DRIVER_CUDA echo   Driver CUDA version: !HM_DRIVER_CUDA!
    echo.
    echo   CUDA recommended for NVIDIA GPUs - the installer downloads the
    echo   llama.cpp build matching your driver's CUDA version automatically.
    echo.
    choice /c CV /m "Choose backend (CUDA recommended / Vulkan)"
    set "BACKEND=cuda"
    if errorlevel 2 set "BACKEND=vulkan"
) else (
    echo   No NVIDIA GPU detected - Vulkan recommended for AMD/Intel.
    echo.
    choice /c VC /m "Choose backend"
    set "BACKEND=vulkan"
    if errorlevel 2 set "BACKEND=cuda"
)
echo   Selected: %BACKEND%
echo.

REM Optional VRAM budget
set "VRAM=8.0"
set /p "VRAM_IN=VRAM in GB [Enter = 8.0]: "
if not "%VRAM_IN%"=="" set "VRAM=%VRAM_IN%"
echo.

REM Optional server port
set "HM_PORT=8001"
set /p "PORT_IN=Server port [Enter = 8001]: "
if not "%PORT_IN%"=="" set "HM_PORT=%PORT_IN%"
echo.

REM Optional SearXNG port (only used if SearXNG is installed in step 6)
set "SEARXNG_PORT=8888"
set /p "SPORT_IN=SearXNG port for web search [Enter = 8888]: "
if not "%SPORT_IN%"=="" set "SEARXNG_PORT=%SPORT_IN%"
echo.

REM Write settings (gpu_backend + vram_budget_gb + server_port; workspace stays EMPTY).
REM Values are passed via environment variables instead of being embedded in
REM the Python code: otherwise cmd's %-signs clash with Python's %-operator.
set "HM_BACKEND=%BACKEND%"
set "HM_VRAM=%VRAM%"
"%PY%" -c "import os, re; from settings import load_settings, save_settings; s = load_settings(); vram_raw = os.environ.get('HM_VRAM', '8.0').replace(',', '.'); _m = re.match(r'\d+(\.\d+)?', vram_raw); s['gpu_backend'] = os.environ.get('HM_BACKEND', 'vulkan'); s['vram_budget_gb'] = float(_m.group(0)) if _m else 8.0; _p = os.environ.get('HM_PORT', '8001').strip(); s['server_port'] = int(_p) if _p.isdigit() else 8001; s.setdefault('workspace', ''); save_settings(s); print('   settings.json written (gpu_backend={}, vram_budget_gb={}, server_port={})'.format(s['gpu_backend'], s['vram_budget_gb'], s['server_port']))"
if errorlevel 1 (
    echo.
    echo   [ERROR] Could not write settings.json.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo.

REM ======================================================
REM [3/6] llama.cpp backend
REM ======================================================
echo  ==========================================================
echo   [3/6] llama.cpp backend
echo  ==========================================================
echo.
echo   Target: %~dp0llama\
echo.
choice /c YN /n /m "Download llama.cpp backend now? [Y/N] "
if errorlevel 2 goto llama_check
"%PY%" deploy\fetch_llamacpp.py --backend %BACKEND%
REM PAREN-IF-FIX (2026-09-01): the old `if errorlevel 1 ( ... )` block put the
REM bare %~dp0llama path INSIDE a parenthesized block. With an install folder
REM like "...\HiveMind_v1.0.3 (4)\" the ')' from the path closed the block early
REM and cmd aborted the batch silently right after a successful llama download.
REM `goto` skips the error lines without a parenthesized block.
if not errorlevel 1 goto llama_check
echo.
echo   [ERROR] Could not download llama.cpp automatically.
echo   Download it manually from https://github.com/ggml-org/llama.cpp/releases
echo   and extract it into %~dp0llama\, then run install.bat again.
echo.
echo   Stopping BEFORE the model download - prevents ~30 GB wasted downloads.
echo.
echo  Press any key to continue... & pause >nul & exit /b 1
:llama_check
set "HAVE_LLAMA="
REM PAREN-PATH-FIX (2026-09-01): a `for /f` over "%~dp0..." breaks when the
REM install folder contains parentheses (e.g. "...\HiveMind_v1.0.3 (3)\") —
REM cmd eats the ')' as the end of the for-block and the batch dies silently.
REM The path is passed to PowerShell via an ENV var so the parens never go
REM through cmd's for-block parser.
set "HM_LLAMA_ROOT=%~dp0llama"
for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "$r = $env:HM_LLAMA_ROOT; if (Get-ChildItem -Recurse -Path $r -Filter llama-server.exe -ErrorAction SilentlyContinue) { '1' }"`) do set "HAVE_LLAMA=%%F"
if defined HAVE_LLAMA (
    echo   [OK] llama-server.exe found.
) else (
    echo   [NOTE] No llama-server.exe found.
    echo   HiveMind will only start once llama.cpp is present -
    echo   but you can already download the models now.
)
echo.

REM ======================================================
REM [4/6] Models
REM ======================================================
echo  ==========================================================
echo   [4/6] Models
echo  ==========================================================
echo.
echo   Default: models are downloaded into %~dp0models.
echo   You can also specify your own models folder.
echo.
set "MODELS_INPUT="
set /p "MODELS_INPUT=Custom folder? [Enter = default]: "
if "%MODELS_INPUT%"=="" goto models_default

call setup_models.bat "%MODELS_INPUT%"
goto searxng_step

:models_default
call setup_models.bat
goto searxng_step

:searxng_step

REM ======================================================
REM [5/6] SearXNG (optional)
REM ======================================================
echo.
echo  ==========================================================
echo   [5/6] SearXNG (web search, requires Docker Desktop)
echo  ==========================================================
echo.
where docker >nul 2>&1
if errorlevel 1 (
    echo   Docker not found - web search stays disabled.
    echo   Can be installed later: searxng.bat
    echo.
) else (
    choice /c YN /n /m "Set up SearXNG now? [Y/N] "
    if !errorlevel! equ 1 call searxng.bat install %SEARXNG_PORT%
)

echo.
REM ======================================================
REM [6/6] Desktop shortcut (optional, HiveMind icon)
REM ======================================================
echo.
echo  ==========================================================
echo   [6/6] Desktop shortcut
echo  ==========================================================
echo.
echo   Creates "HiveMind.lnk" on the Desktop that starts
echo   start_hivemind.bat with the HiveMind icon.
echo.
choice /c YN /n /m "Create a Desktop shortcut with the HiveMind icon? [Y/N] "
if errorlevel 2 goto shortcut_done
call create_shortcut.bat
:shortcut_done
echo.

echo  ==============================================================
echo   Installation finished!
echo.
echo   Start:        start_hivemind.bat
echo   UI:           http://localhost:%HM_PORT%
echo.
echo   IMPORTANT: Before the first run, set a workspace
echo   in the UI (field "Workspace") - there is no default.
echo  ==============================================================
echo.
echo  Press any key to continue...
pause >nul