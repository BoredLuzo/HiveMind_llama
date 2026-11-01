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

REM --- browser tool ----------------------------------------------------------
set "HAVE_BROWSER="
for /d %%D in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do set "HAVE_BROWSER=1"
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
if defined MISS_STT     echo   [missing] speech to text    - deploy\setup_voice.bat
if defined MISS_TTS     echo   [missing] text to speech    - deploy\setup_voice.bat --tts
if defined MISS_DEC     echo   [missing] routing decider   - deploy\fetch_decider.py
if defined MISS_BROWSER echo   [missing] browser tool      - playwright install chromium
if defined MISS_POSIX   echo   [missing] POSIX tools       - winget install CoreUtils
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
    echo   The TTS model is behind a Hugging Face gate. If the download
    echo   asks for a token: accept the terms on the model page and run
    echo   "uvx --from huggingface_hub hf auth login" once.
    call "%~dp0setup_voice.bat" --tts
)
if defined MISS_DEC (
    echo.
    echo  --- routing decider ----------------------------------------
    "%PY%" deploy\fetch_decider.py
    if exist "requirements-decider.txt" (
        "%PY%" -m pip install -q -r requirements-decider.txt 2>nul
        if errorlevel 1 echo   [WARN] pip install of requirements-decider.txt failed.
    )
)
if defined MISS_BROWSER (
    echo.
    echo  --- browser tool -------------------------------------------
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

echo.
echo  Optional components handled.
echo.
endlocal
goto :eof
