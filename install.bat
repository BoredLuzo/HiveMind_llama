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
echo    [1/6] Python 3.14       - found or installed automatically
echo    [2/6] Python packages   - installed into a virtual env (.venv)
echo    [3/6] GPU backend       - CUDA (NVIDIA) or Vulkan (AMD/Intel)
echo    [4/6] llama.cpp backend - downloaded for the chosen backend
echo    [5/6] Models            - downloaded into \models
echo    [6/6] SearXNG           - optional web search (requires Docker)
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
REM [1/6] Python 3.14
REM ======================================================
echo  ==========================================================
echo   [1/6] Checking Python
echo  ==========================================================
echo.
set "PY="

REM Prefer the py launcher (3.14 first, then any 3.x) and resolve it to the
REM real python.exe so "%PY%" can always be quoted safely.
py -3.14 --version >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%X in ('py -3.14 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%X"
)
if not defined PY py -3 --version >nul 2>&1
if not defined PY if not errorlevel 1 (
    for /f "delims=" %%X in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%X"
)

REM python / python3 on PATH
if not defined PY python --version >nul 2>&1
if not defined PY if not errorlevel 1 set "PY=python"
if not defined PY python3 --version >nul 2>&1
if not defined PY if not errorlevel 1 set "PY=python3"

REM Known install locations (no launcher, incomplete PATH)
if not defined PY if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PY if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"

if defined PY goto python_ok

REM ---- Install Python (winget first, then python.org) ----
echo   Python 3.14 not found - it will be installed.
echo   Target: %LocalAppData%\Programs\Python\Python314\
choice /c YN /n /m "Install Python 3.14 now? [Y/N] "
if errorlevel 2 (
    echo.
    echo   [ERROR] HiveMind cannot be installed without Python.
    echo   Download it manually: https://www.python.org/downloads/
    echo   Important: enable "Add python.exe to PATH" during installation.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
where winget >nul 2>&1
if not errorlevel 1 (
    winget install --id Python.Python.3.14 --accept-source-agreements --accept-package-agreements --silent --override "/quiet InstallAllUsers=0 PrependPath=1"
    if errorlevel 1 (
        echo   [ERROR] winget installation failed.
        echo.
        echo  Press any key to continue... & pause >nul & exit /b 1
    )
) else (
    echo   No winget - downloading the installer from python.org...
    curl.exe -L --fail -o "%TEMP%\python314.exe" "https://www.python.org/ftp/python/3.14.0/python-3.14.0-amd64.exe" >nul 2>&1
    if errorlevel 1 (
        echo   curl failed - trying PowerShell with TLS 1.2...
        powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('https://www.python.org/ftp/python/3.14.0/python-3.14.0-amd64.exe','%TEMP%\python314.exe')"
    )
    if not exist "%TEMP%\python314.exe" (
        echo   [ERROR] Python download failed.
        echo   Download it manually: https://www.python.org/downloads/
        echo   Important: enable "Add python.exe to PATH" during installation.
        echo.
        echo  Press any key to continue... & pause >nul & exit /b 1
    )
    "%TEMP%\python314.exe" /quiet InstallAllUsers=0 PrependPath=1
    del "%TEMP%\python314.exe" 2>nul
    set "PATH=%PATH%;%LocalAppData%\Programs\Python\Python314;%LocalAppData%\Programs\Python\Python314\Scripts"
)

REM Verify the installation
if not defined PY if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PY if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
if not defined PY (
    echo   [ERROR] Python installation could not be verified.
    echo   Close this window, open a new terminal and run install.bat again.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

:python_ok
echo   OK: %PY%
echo.

REM ======================================================
REM uv (fast Python package manager)
REM ======================================================
echo  ==========================================================
echo   [uv] Fast package manager
echo  ==========================================================
echo.
where uv >nul 2>&1
if errorlevel 1 (
    echo  [uv] uv not found - installing...
    echo  [uv] Target: %USERPROFILE%\.local\bin (added to PATH)
    where winget >nul 2>&1
    if not errorlevel 1 (
        winget install --id astral-sh.uv --accept-source-agreements --accept-package-agreements --silent
    ) else (
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    )
    set "PATH=%PATH%;%USERPROFILE%\.local\bin"
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
echo.

REM ======================================================
REM Virtual environment (keeps the system Python clean)
REM ======================================================
echo  ==========================================================
echo   [venv] Virtual environment
echo  ==========================================================
echo.
if not exist ".venv\Scripts\python.exe" (
    echo  [venv] Creating virtual environment .venv with uv ...
    uv venv -p "%PY%" .venv
    if errorlevel 1 (
        echo.
        echo   [ERROR] Could not create the virtual environment.
        echo.
        echo  Press any key to continue... & pause >nul & exit /b 1
    )
)
set "PY=.venv\Scripts\python.exe"
echo  [venv] %CD%\.venv
echo.

REM ======================================================
REM [2/6] Dependencies
REM ======================================================
echo  ==========================================================
echo   [2/6] Installing Python packages
echo  ==========================================================
echo.
echo   Target: %CD%\.venv
echo.
choice /c YN /n /m "Install Python packages into .venv? [Y/N] "
if errorlevel 2 goto pip_skip
uv pip install -p ".venv\Scripts\python.exe" -r requirements.txt
if errorlevel 1 (
    echo.
    echo   [ERROR] Package installation failed - read the message above.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo   OK.
:pip_skip
echo.

REM ======================================================
REM [3/6] GPU backend (auto-detection)
REM ======================================================
echo  ==========================================================
echo   [3/6] GPU backend
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
REM [4/6] llama.cpp backend
REM ======================================================
echo  ==========================================================
echo   [4/6] llama.cpp backend
echo  ==========================================================
echo.
echo   Target: %~dp0llama\
echo.
choice /c YN /n /m "Download llama.cpp backend now? [Y/N] "
if errorlevel 2 goto llama_check
"%PY%" deploy\fetch_llamacpp.py --backend %BACKEND%
if errorlevel 1 (
    echo.
    echo   [ERROR] Could not download llama.cpp automatically.
    echo   Download it manually from https://github.com/ggml-org/llama.cpp/releases
    echo   and extract it into %~dp0llama\, then run install.bat again.
    echo.
    echo   Stopping BEFORE the model download - prevents ~30 GB wasted downloads.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
:llama_check
set "HAVE_LLAMA="
for /f %%i in ('dir /b /s "%~dp0llama\llama-server.exe" 2^>nul') do set "HAVE_LLAMA=1"
if defined HAVE_LLAMA (
    echo   [OK] llama-server.exe found.
) else (
    echo   [NOTE] No llama-server.exe found.
    echo   HiveMind will only start once llama.cpp is present -
    echo   but you can already download the models now.
)
echo.

REM ======================================================
REM [5/6] Models
REM ======================================================
echo  ==========================================================
echo   [5/6] Models
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
REM [6/6] SearXNG (optional)
REM ======================================================
echo.
echo  ==========================================================
echo   [6/6] SearXNG (web search, requires Docker Desktop)
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