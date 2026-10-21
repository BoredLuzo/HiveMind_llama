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
echo    [1/9] Python            - set up python environment
echo    [2/9] GPU backend       - CUDA (NVIDIA) or Vulkan (AMD/Intel)
echo    [3/9] llama.cpp backend - downloaded for the chosen backend
echo    [4/9] Windows tools     - optional POSIX tools (grep/sed/find)
echo    [5/9] Desktop shortcut  - optional, with the HiveMind icon
echo    [6/9] Models            - downloaded into your chosen folder
echo    [7/9] SearXNG           - optional web search (requires Docker)
echo    [8/9] Voice             - optional speech to text / text to speech
echo    [9/9] Telegram gateway  - optional: message HiveMind from
echo                              your phone via your own bot
echo                              (needs a @BotFather token, 2FA on)
echo.

echo   [Y] full install    [N] abort    [O] only the optional parts
echo       (SearXNG / Voice / Telegram - for an existing installation,
echo        or to add them to one you installed a while ago)
echo.
choice /c YNO /n /m "Install HiveMind now? [Y/N/O] "
if errorlevel 3 goto optionals_only
if errorlevel 2 (
    echo.
    echo  Aborted - nothing was installed.
    echo.
    exit /b 0
)
echo.

REM ======================================================
REM [1/9] Python
REM ======================================================
echo  ==========================================================
echo   [1/9] Setting up Python
echo  ==========================================================
echo.

REM ======================================================
REM uv (fast Python package manager)
REM UV-ROBUST (2026-10-14): detection is existence-based over ALL known
REM install locations, and PATH changes are applied OUTSIDE parenthesized
REM blocks (batch expands %VARS% at parse time inside a block, so the old
REM in-block "set PATH + where uv" could never see a fresh install
REM (the ERROR-every-second-run report).
REM ======================================================
echo  ==========================================================
echo   [uv] Setting up package manager
echo  ==========================================================
echo.
set "UV_DIR="
where uv >nul 2>&1
if not errorlevel 1 set "UV_DIR=on_path"
if not defined UV_DIR if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_DIR=%USERPROFILE%\.local\bin"
if not defined UV_DIR if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV_DIR=%LOCALAPPDATA%\Microsoft\WinGet\Links"

if not defined UV_DIR (
    echo  [uv] uv not found - installing...
    echo  [uv] Target: %USERPROFILE%\.local\bin (added to PATH)
    where winget >nul 2>&1
    if not errorlevel 1 (
        REM --source winget supresses the MS store license agreement prompt
        winget install --id astral-sh.uv --source winget --silent
    ) else (
        powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    )
)

if not defined UV_DIR if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_DIR=%USERPROFILE%\.local\bin"
if not defined UV_DIR if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV_DIR=%LOCALAPPDATA%\Microsoft\WinGet\Links"
where uv >nul 2>&1
if not errorlevel 1 set "UV_DIR=on_path"

if not defined UV_DIR (
    echo.
    echo   [ERROR] uv could not be installed.
    echo   Install it manually: https://docs.astral.sh/uv/
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
if not "%UV_DIR%"=="on_path" set "PATH=%PATH%;%UV_DIR%"
if not "%UV_DIR%"=="on_path" set "UV_PATH_ADDED=1"
echo   [uv] ok: %UV_DIR%
REM UV-NEW-SHELL (2026-10-17): this PATH fix lives in THIS process only. A
REM user who installed uv just now (winget / astral) has it in the registry
REM PATH, but any OTHER terminal that was already open - and the one they
REM open next from an old shortcut - may not see it yet, which is exactly the
REM "uv is installed but not in PATH" report. Say it instead of letting them
REM hit "uv is not recognized" later.
if not "%UV_DIR%"=="on_path" (
    echo   [uv] NOTE: uv was added to THIS window's PATH only.
    echo   [uv] NOTE: open a NEW terminal before running uv yourself
    echo   [uv] NOTE: ^(or log off/on^) so your shell sees the new PATH.
)
uv --version

SET "UV_NO_DEV=1"

REM PYTHON-VIA-UV (2026-09-09): provision the interpreter explicitly instead
REM of relying on a system Python. A fresh machine then needs NOTHING
REM preinstalled but this installer - uv downloads a managed CPython 3.14
REM (matching pyproject's requires-python) and `uv sync` uses it for the venv.
echo  ==========================================================
echo   [python] Ensuring Python 3.14 (managed by uv)
echo  ==========================================================
echo.
uv python install 3.14
if errorlevel 1 (
    REM A foreign python3.14.exe (e.g. a manual install in %USERPROFILE%\
    REM .local\bin) blocks uv from placing its managed copy. --force takes
    REM the name over; the interpreter version stays 3.14 either way.
    echo   [python] retrying with --force (an unmanaged python3.14.exe exists)
    uv python install 3.14 --force
)
REM Hard gate: without a usable 3.14 the next step would silently pick
REM another interpreter (3.15 has no wheel for tree-sitter -> MSVC build
REM -> the install died here on a fresh machine, 2026-10-18).
uv python find 3.14 >nul 2>&1
if errorlevel 1 goto python_missing
echo.

REM ======================================================
REM Virtual environment (keeps the system Python clean)
REM ======================================================
echo  ==========================================================
echo   [venv] Setting up virtual environment
echo  ==========================================================
echo.
if not exist "%~dp0.venv" goto venv_ok
if exist "%~dp0.venv\Scripts\python.exe" goto venv_ok
echo   [venv] removing a broken leftover .venv (no python.exe inside)
rmdir /s /q "%~dp0.venv" >nul 2>&1
:venv_ok

REM SYNC-FLOW (2026-09-09): goto-based instead of parenthesized blocks - the
REM paths involved (%~dp0, %PY%) break cmd's block parser when the install
REM folder name contains parentheses (see PAREN-PATH-FIX below).
if exist "%~dp0pyproject.toml" goto uv_sync_project

REM RELEASE-FALLBACK: older release zips ship requirements.txt only (pre-uv
REM packaging). uv sync needs pyproject.toml and dies with "No pyproject.toml
REM found" - fall back to a plain venv + requirements install.
echo   [uv] pyproject.toml not found - using requirements.txt fallback.
uv venv --python 3.14
if errorlevel 1 goto uv_failed
uv pip install --python "%~dp0.venv\Scripts\python.exe" -r requirements.txt
if errorlevel 1 goto uv_failed
goto uv_done

:uv_sync_project
REM --frozen (2026-10-18): without a lockfile every install resolved the
REM newest package of the day - one machine got pydantic 2.14.1/2.50.1 and
REM another a different pair. uv.lock ships with the zip and pins all 64.
uv sync --all-extras --frozen --python 3.14
if errorlevel 1 goto uv_failed

:uv_done
goto venv_py_check

:python_missing
echo.
echo   [ERROR] No usable Python 3.14.
echo   uv could not provision it. Two ways out:
echo     1. Delete %USERPROFILE%\.local\bin\python3.14.exe (a foreign
echo        copy blocks uv) and run install.bat again.
echo     2. Install Python 3.14 from python.org, then run install.bat.
echo.
echo  Press any key to continue... & pause >nul & exit /b 1

:python_wrong_in_venv
echo.
echo   [ERROR] The virtual environment was built with the wrong Python:
for /f "usebackq delims=" %%V in (`"%~dp0.venv\Scripts\python.exe" -c "import sys;print(sys.version.split()[0])"`) do echo          found %%V
echo   HiveMind needs 3.14 (compiled dependencies only ship wheels up to
echo   that version). Fix: delete the .venv folder, then run install.bat
echo   again - it now pins the interpreter instead of taking the newest.
echo.
echo  Press any key to continue... & pause >nul & exit /b 1

:uv_failed
echo.
echo   [ERROR] Dependency installation failed (uv).
echo   Check your internet connection and run install.bat again.
echo.
echo  Press any key to continue... & pause >nul & exit /b 1

:venv_py_check
REM PYWIN32 (2026-10-18): the wheel registers itself through pywin32.pth +
REM pywin32_system32/ - no postinstall step is needed or shipped in a uv venv.
REM The venv must be 3.14. A silent fallback (3.15) breaks compiled
REM dependencies that only ship wheels up to 3.14.
"%~dp0.venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)" >nul 2>&1
if errorlevel 1 goto python_wrong_in_venv

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

REM VENV-PY-FIX (2026-09-09): `uv python find` returns a managed/system
REM interpreter, NOT necessarily the project venv. The venv python is the
REM deterministic choice - it has the synced dependencies (httpx for
REM fetch_llamacpp.py, settings imports for the settings writer below).
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto py_ok
echo.
echo   [ERROR] .venv not found after dependency install - install.bat cannot continue.
echo.
echo  Press any key to continue... & pause >nul & exit /b 1

:py_ok

REM ======================================================
REM [2/9] GPU backend (auto-detection)
REM ======================================================
echo  ==========================================================
echo   [2/9] GPU backend
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
REM I4 (audit r3): a REPAIR re-run must keep the values the user chose
REM the first time - seed the shown defaults from the existing settings.
set "VRAM="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "$s = if (Test-Path settings.json) { Get-Content settings.json -Raw | ConvertFrom-Json } else { $null }; Write-Output $s.vram_budget_gb"`) do set "VRAM=%%V"
if not defined VRAM set "VRAM=8.0"
set /p "VRAM_IN=VRAM in GB [Enter = keep %VRAM%]: "
if defined VRAM_IN set "VRAM=!VRAM_IN!"
echo.

REM Optional server port
set "HM_PORT="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$s = if (Test-Path settings.json) { Get-Content settings.json -Raw | ConvertFrom-Json } else { $null }; Write-Output $s.server_port"`) do set "HM_PORT=%%P"
if not defined HM_PORT set "HM_PORT=8001"
set /p "PORT_IN=Server port [Enter = keep %HM_PORT%]: "
if defined PORT_IN set "HM_PORT=!PORT_IN!"
echo.

REM SearXNG port: fixed default, only relevant if SearXNG is set up later -
REM not worth a prompt during install (PROMPT-DIET 2026-09-12).
set "SEARXNG_PORT=8888"

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
REM [3/9] llama.cpp backend
REM ======================================================
echo  ==========================================================
echo   [3/9] llama.cpp backend
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
REM install folder contains parentheses (e.g. "...\HiveMind_v1.0.3 (3)\") -
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
REM [4/9] Windows tools (optional: POSIX tools for the agent)
REM Models are trained on GNU coreutils and write grep/sed/awk/find.
REM Microsoft Core Utils are the official port. Optional, never fatal:
REM without them the coder prompt points at the built-in tools.
REM ======================================================
echo  ==========================================================
echo   [4/9] Windows tools (optional)
echo  ==========================================================
echo.
echo   Coding models are trained on Linux and write things like
echo     grep -rn "foo" src/
echo     head -50 build.log
echo   On Windows those commands do not exist. Microsoft Core Utils
echo   are the official port and make them work.
echo.
set "HAVE_COREUTILS="
where grep >nul 2>&1
if not errorlevel 1 set "HAVE_COREUTILS=1"
if not defined HAVE_COREUTILS goto coreutils_ask
echo   [OK] POSIX tools are already available - nothing to do.
goto coreutils_done
:coreutils_ask
set "HAVE_WINGET="
where winget >nul 2>&1
if not errorlevel 1 set "HAVE_WINGET=1"
if not defined HAVE_WINGET goto coreutils_nowinget
echo   Install Microsoft Core Utils now (~5 MB, from the Microsoft Store)?
choice /c YN /n /m "Install Core Utils? [Y/N] "
if errorlevel 2 goto coreutils_skipped
echo.
winget install --id Microsoft.CoreUtils -e --silent --accept-package-agreements --accept-source-agreements
where grep >nul 2>&1
if errorlevel 1 goto coreutils_failed
echo.
echo   [OK] Core Utils installed. The agent can use grep/sed/awk/find now.
goto coreutils_done
:coreutils_failed
echo.
echo   [NOTE] The install did not complete. Nothing is broken:
echo   HiveMind tells the coder to use its own tools instead.
echo   Retry later with:  winget install Microsoft.CoreUtils
goto coreutils_done
:coreutils_nowinget
echo   [NOTE] winget is not available, so Core Utils cannot be installed
echo   automatically. HiveMind works without them.
echo   Manual: https://learn.microsoft.com/en-us/windows/core-utils/overview
goto coreutils_done
:coreutils_skipped
echo   Skipped - add it later with: winget install Microsoft.CoreUtils
:coreutils_done
echo.

REM [5/9] Desktop shortcut (optional, HiveMind icon)
REM SHORTCUT-FIRST (2026-09-12): asked BEFORE the long network steps,
REM so a fresh install always gets the icon even if the user aborts
REM during model downloads.
REM ======================================================
echo  ==========================================================
echo   [5/9] Desktop shortcut
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

REM ======================================================
REM [6/9] Models
REM ======================================================
echo  ==========================================================
echo   [6/9] Models
echo  ==========================================================
echo.
REM SKIP-PROMPT (2026-10-16): every optional step asks first - Models was
REM the only one that dropped you straight into the menu.
choice /c YN /n /m "Set up / register models now? [Y/N] "
if errorlevel 2 (
    echo   Skipped - run setup_models.bat anytime.
    goto decider_model_step
)
REM No folder prompt here - setup_models.bat asks ONCE (download target;
REM existing GGUFs in that folder get registered automatically).
call setup_models.bat

REM [6b] Routing decider model (optional pre-download) -----------------------
REM OPT-IN MODEL (2026-10-16): the decider is OFF by default; when enabled
REM later, the engine downloads fastino/GLiNER2.5-Decide (~1-2 GB) from the
REM Hugging Face hub on first use. Pre-warming here makes that first enable
REM %  instant. Skippable like every other optional; a failure here never
REM %  aborts the install.
:decider_model_step
echo.
echo   Routing decider (optional): a small CPU classifier that picks
echo   direct answer vs. code run. OFF by default - enable it later in
echo   Settings -^> Routing. It needs the gliner2 package + CPU torch
echo   (~2.5 GB env) plus the model (~1-2 GB, Hugging Face).
choice /c YN /n /m "Install the decider environment + model now? [Y/N] "
if errorlevel 2 (
    echo   Skipped - install later with: uv pip install --python
    echo   .venv\Scripts\python.exe gliner2 torch huggingface_hub
    goto searxng_step
)
REM DECIDER-ENV (2026-10-21, audit): the model weights alone are useless
REM %  without the gliner2 package and CPU torch - the first version of this
REM %  step downloaded only the weights and the decider still fell back to
REM %  rules. CPU wheel index keeps torch lean (~200 MB instead of the CUDA
REM %  monolith); failures degrade gracefully (rules fallback at runtime).
uv pip install --python "%PY%" --index-url https://download.pytorch.org/whl/cpu torch --quiet
if errorlevel 1 uv pip install --python "%PY%" torch --quiet
uv pip install --python "%PY%" gliner2 transformers huggingface_hub --quiet
"%PY%" -c "from huggingface_hub import snapshot_download; snapshot_download('fastino/GLiNER2.5-Decide')"
if not errorlevel 1 (
    echo   Decider environment + model ready - enabling the decider later
    echo   is instant (Settings -^> Routing -^> Decider: on).
) else (
    echo   [NOTE] Pre-download failed (offline or wheel missing).
    echo          The engine falls back to keyword rules until installed.
)
goto searxng_step

:searxng_step

REM ======================================================
REM [7/9] SearXNG (optional)
REM ======================================================
echo.
echo  ==========================================================
echo   [7/9] SearXNG (web search, requires Docker Desktop)
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

echo  ==========================================================
echo   [8/9] Voice (optional: speech to text / text to speech)
echo  ==========================================================
echo.
echo   Local speech-to-text turns a Telegram voice note (or the
echo   UI microphone) into text. Nothing is installed by default;
echo   the engine + model are ~420 MB and CPU only (English only).
echo.
echo   This step is OPTIONAL and can be run at any time later -
echo   NO reinstall needed, nothing else in the install changes:
echo     - Settings -^> Voice -^> Download  (UI, with progress)
echo     - or deploy\setup_voice.bat [model] [--tts^|--both]
echo   Text-to-speech is separate and needs a one-time
echo   "hf auth login" (gated model) - see docs\voice_setup.md
echo   token step by step: docs\huggingface_token.md
echo.
echo   [1] Speech to text only              (~420 MB, English)
echo   [2] Speech to text + text to speech  (adds ~200 MB PyTorch, needs hf login)
echo   [3] Text to speech only              (~200 MB PyTorch, needs hf login)
echo   [4] Skip (recommended if unsure) - add it later without reinstalling:
echo       Settings -^> Voice, or deploy\setup_voice.bat
echo.
set "VOICE_PICK="
set /p VOICE_PICK=Choose [1-4, default 4]: 
if not defined VOICE_PICK set "VOICE_PICK=4"
if "%VOICE_PICK%"=="1" call "%~dp0deploy\setup_voice.bat"
if "%VOICE_PICK%"=="2" call "%~dp0deploy\setup_voice.bat" --both
if "%VOICE_PICK%"=="3" call "%~dp0deploy\setup_voice.bat" --tts
if "%VOICE_PICK%"=="4" echo   Skipped - add it later with no reinstall: Settings -^> Voice, or deploy\setup_voice.bat.
echo.

echo  ==========================================================
echo   [9/9] Telegram gateway (optional)
echo  ==========================================================
echo.
echo   Control HiveMind from your phone via your own Telegram bot.
echo   You need a bot token from @BotFather in Telegram (2-3 min)
echo   and two-step verification ON for your Telegram account.
echo   The gateway is OFF until you set it up - skipping is fine
echo   (set it up anytime: start_gateway.bat setup).
echo.
choice /c YN /n /m "Set up the Telegram gateway now? [Y/N] "
if errorlevel 2 goto tg_done
call start_gateway.bat setup
:tg_done
echo.

echo  ==============================================================
echo   Installation finished!
echo.
echo   Start:        start_hivemind.bat
echo   UI:           http://localhost:%HM_PORT%
echo   Optional:     Telegram gateway - start_gateway.bat setup
echo                 (docs\gateway_setup.md, off by default)
echo   Optional:     Voice - deploy\setup_voice.bat [--tts^|--both]
echo                 (no reinstall needed, or use Settings -^> Voice)
echo.
echo   IMPORTANT: Before the first run, set a workspace
echo   in the UI (field "Workspace") - there is no default.
if defined UV_PATH_ADDED (
echo.
echo   NOTE: uv was installed into %UV_DIR% and that folder was added to
echo   NOTE: your PATH. If you want to run "uv" yourself in a terminal,
echo   NOTE: open a NEW terminal first (this window has the PATH, others
echo   NOTE: opened before the install may not see it.
)
echo  ==============================================================
echo.
echo  Press any key to continue...
pause >nul
exit /b 0

REM ======================================================
REM  OPTIONALS ONLY -- reached with [O] at the first prompt.
REM  Jumps straight to step [7/9] and runs SearXNG, Voice and
REM  the Telegram gateway. Python, GPU backend, llama.cpp and
REM  the models are skipped: this is the "add a part later"
REM  path, no reinstall and no re-download of anything big.
REM  Written with labels instead of if-blocks on purpose -
REM  parenthesised blocks choke on text that contains ")".
REM ======================================================
:optionals_only
echo.
echo  ==========================================================
echo   Optional parts only
echo  ==========================================================
echo.
echo   Skipping: Python, GPU backend, llama.cpp, models.
echo   Running : SearXNG, Voice, Telegram gateway.
echo.

REM Ports are normally set by the full-install steps; restore them here so
REM searxng.bat and the final summary do not get an empty value.
if defined SEARXNG_PORT goto opt_have_sx_port
set "SEARXNG_PORT=8888"
:opt_have_sx_port
if defined HM_PORT goto opt_have_port
set "HM_PORT="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$s = if (Test-Path settings.json) { Get-Content settings.json -Raw | ConvertFrom-Json } else { $null }; Write-Output $s.server_port"`) do set "HM_PORT=%%P"
if not defined HM_PORT set "HM_PORT=8001"
:opt_have_port

if exist "%~dp0.venv\Scripts\python.exe" goto opt_have_venv
echo   [NOTE] No Python environment in this folder. SearXNG works on its own,
echo          but Voice and the Telegram gateway need the full install first
echo          (step 1/8). Everything below will tell you if it is missing.
echo.
:opt_have_venv

goto searxng_step