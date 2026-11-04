@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  check_optionals.bat - offer the optional components an update does not
REM  install by itself.
REM
REM  Called by update.bat after the dependency refresh. Only components that
REM  are NOT present on this machine are offered; a fully equipped install
REM  just says so and returns. Every install is failure-tolerant: a missing
REM  network, a closed Hugging Face gate or an absent uv never fails the
REM  update itself.
REM
REM  Detected here:
REM    speech to text   stt\bin\*.exe + stt\models\*
REM    text to speech   Hugging Face cache entry for the gated TTS model
REM    routing decider  models\decider\gliner25-decide\*
REM    browser tool     Playwright Chromium under %LOCALAPPDATA%\ms-playwright
REM    POSIX tools      only when neither Git Bash nor Core Utils is present
REM ===========================================================================
cd /d "%~dp0.."

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM PREFER-UV (2026-11-04): uv-created venvs often have no pip. The update
REM itself refreshes deps through uv; optional installs must use the same
REM tool instead of failing silently on "No module named pip".
set "UV_EXE="
for /f "usebackq delims=" %%U in (`where uv 2^>nul`) do (
    if not defined UV_EXE set "UV_EXE=%%U"
)
if not defined UV_EXE if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"

set "MISS_STT="
set "MISS_TTS="
set "MISS_DEC="
set "MISS_BROWSER="
set "MISS_POSIX="

REM --- speech to text: binaries and at least one model ------------------------
set "HAVE_STT_BIN="
for %%F in ("stt\bin\*.exe") do if exist "%%~F" set "HAVE_STT_BIN=1"
set "HAVE_STT_MODEL="
for %%F in ("stt\models\*") do if exist "%%~F" set "HAVE_STT_MODEL=1"
if not defined HAVE_STT_BIN set "MISS_STT=1"
if not defined HAVE_STT_MODEL set "MISS_STT=1"

REM --- text to speech: the gated model lives in the HF cache ------------------
set "HF_CACHE=%USERPROFILE%\.cache\huggingface\hub"
set "HAVE_TTS="
if exist "%HF_CACHE%\models--kyutai--pocket-tts*" set "HAVE_TTS=1"
if not defined HAVE_TTS if exist "%HF_HOME%\hub\models--kyutai--pocket-tts*" set "HAVE_TTS=1"
if not defined HAVE_TTS set "MISS_TTS=1"

REM --- routing decider -------------------------------------------------------
set "DEC_DIR=models\decider\gliner25-decide"
set "HAVE_DEC="
for %%F in ("%DEC_DIR%\*") do if exist "%%~F" set "HAVE_DEC=1"
if not defined HAVE_DEC set "MISS_DEC=1"
REM MODEL-ONLY is not enough: a 1.5.2 install can have the downloaded
REM GLiNER2 model while gliner2/PyTorch is still missing. Check the import
REM too, or the optional install is never offered again.
"%PY%" -c "import gliner2" >nul 2>&1
if errorlevel 1 set "MISS_DEC=1"

REM --- browser tool ----------------------------------------------------------
REM A dependency refresh can bump playwright past the installed browser build
REM (then every launch fails with "Executable doesn't exist ..."). Comparing
REM directory names cannot catch that, so the check asks playwright itself:
REM when the package is there, run the idempotent install (no download when
REM the build already matches).
set "HAVE_BROWSER="
"%PY%" -c "import playwright" >nul 2>&1
if not errorlevel 1 set "HAVE_BROWSER=1"
if not defined HAVE_BROWSER set "MISS_BROWSER=1"

REM --- POSIX tools: Git Bash covers it already -------------------------------
set "HAVE_POSIX="
where grep >nul 2>&1
if not errorlevel 1 set "HAVE_POSIX=1"
if exist "%ProgramFiles%\Git\usr\bin\grep.exe" set "HAVE_POSIX=1"
if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\grep.exe" set "HAVE_POSIX=1"
if not defined HAVE_POSIX set "MISS_POSIX=1"

set "MISS_COUNT=0"
for %%V in (MISS_STT MISS_TTS MISS_DEC MISS_BROWSER MISS_POSIX) do (
    if defined %%V set /a MISS_COUNT+=1
)

echo.
echo  ==========================================================
echo   Optional components
echo  ==========================================================
echo.
if "%MISS_COUNT%"=="0" (
    echo   Everything optional is already installed:
    echo     speech to text, text to speech, routing decider,
    echo     browser tool, POSIX tools.
    echo.
    goto :eof
)
if defined MISS_STT     echo   [missing] speech to text    - local Whisper/Parakeet binaries + model
if defined MISS_TTS     echo   [missing] text to speech    - gated Pocket TTS model
if defined MISS_DEC     echo   [missing] routing decider   - GLiNER2 model + gliner2[local]/PyTorch ^(~2.5 GB^)
if defined MISS_BROWSER echo   [missing] browser tool      - Playwright Chromium
if defined MISS_POSIX   echo   [missing] POSIX tools       - Git Bash or Microsoft CoreUtils
echo.
if defined MISS_DEC (
    echo   Routing decider needs the model file AND the Python package
    echo   "gliner2[local]" with PyTorch. The model download alone is not
    echo   enough; installer and updater install it through uv. If uv is
    echo   missing, run install.bat once to provision it.
)
echo.
if /i "%~1"=="--dry-run" (
    echo   [dry run] nothing installed, %MISS_COUNT% component^(s^) missing.
    echo.
    goto :eof
)
choice /c YN /n /m "Install the missing optional components now? [Y/N] "
if errorlevel 2 (
    echo.
    echo   Skipped. Add them later with the commands above.
    echo.
    goto :eof
)

if defined MISS_STT (
    echo.
    echo  --- speech to text -----------------------------------------
    call "%~dp0setup_voice.bat"
)
if defined MISS_TTS (
    echo.
    echo  --- text to speech -----------------------------------------
    echo   Text to speech uses a gated model ^(Pocket TTS^).
    echo   The setup below walks you through the one-time Hugging Face
    echo   login: accept the terms, create a Read token, paste it when
    echo   asked. HiveMind never creates a token; public models need none.
    call "%~dp0setup_voice.bat" --tts
)
if defined MISS_DEC (
    echo.
    echo  --- routing decider ----------------------------------------
    echo   Model: fastino/GLiNER2.5-Decide
    echo   Package: gliner2[local] + PyTorch ^(about 2.5 GB download^)
    "%PY%" deploy\fetch_decider.py
    if exist "requirements-decider.txt" (
        if defined UV_EXE (
            echo   Installing with uv: "%UV_EXE%" pip install --python "%PY%"
            "%UV_EXE%" pip install --python "%PY%" -r requirements-decider.txt
            if errorlevel 1 (
                echo   [WARN] routing decider package install failed.
                echo          The model is downloaded, but gliner2/PyTorch is not usable.
                echo          Run this yourself to see the full error:
                echo            "%UV_EXE%" pip install --python "%PY%" -r requirements-decider.txt
            ) else (
                echo   [OK] routing decider package installed.
            )
        ) else (
            echo   [WARN] uv is not available - the routing decider package was not installed.
            echo          Run install.bat once ^(it provisions uv^), then re-run update.bat.
        )
    )
)
if defined MISS_BROWSER (
    echo.
    echo  --- browser tool -------------------------------------------
    REM idempotent: installs only what this playwright build is missing
    "%PY%" -m playwright install chromium
    if errorlevel 1 echo   [WARN] Chromium download failed - the browser tool stays off.
)
if defined MISS_POSIX (
    echo.
    echo  --- POSIX tools --------------------------------------------
    echo   Installing Microsoft Core Utils ^(~5 MB, Microsoft Store^)...
    winget install --id Microsoft.CoreUtils -e --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 winget install --source msstore --id 9NBLGGH4R32N --accept-package-agreements --accept-source-agreements
    where grep >nul 2>&1
    if errorlevel 1 echo   [NOTE] Core Utils may need a fresh login before grep is on PATH.
)

REM BROWSER-MATCH (2026-11-02): an update refreshes requirements, which can
REM bump playwright past the installed browser build - then every launch fails
REM with "Executable doesn't exist ... chromium_headless_shell-<new>". This run
REM is idempotent (no download when the build matches) and cheap, so it always
REM happens, not only when the component looks missing.
if defined HAVE_BROWSER (
    echo.
    echo  --- browser engine -------------------------------------------
    "%PY%" -m playwright install chromium
    if errorlevel 1 echo   [WARN] Browser refresh failed - the browser tool stays off.
)

echo.
echo  Optional components handled.
echo.
endlocal
goto :eof
