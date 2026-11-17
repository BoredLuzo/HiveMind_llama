@echo off
REM ===========================================================================
REM  setup_voice.bat - optional voice downloads (speech in / speech out)
REM
REM  HiveMind runs without any of this. Two independent parts:
REM    (default)  speech-to-text: whisper.cpp / Parakeet binaries + one model
REM    --tts      text-to-speech: pre-warms the Pocket TTS environment (PyTorch
REM               into uv's cache) and fetches the gated model. Needs a one-time
REM               "hf auth login" - without it the script explains and stops.
REM    --both     both of the above
REM
REM  Usage:  deploy\setup_voice.bat [model] [--tts|--woof|--both]
REM          deploy\setup_voice.bat base --tts
REM  Models: parakeet-q4k (default, 396 MB) | base (141 MB) | tiny (74 MB) | small (465 MB)
REM  Options: --tts (Pocket TTS), --woof (Underdog Woof CPU), --both, --help|-h
REM
REM  NOTE: label based on purpose - parenthesised if-blocks in cmd.exe choke on
REM  text that itself contains ")" (the first version printed half a line and
REM  silently skipped the TTS part).
REM ===========================================================================
setlocal DisableDelayedExpansion
cd /d "%~dp0.."

set "PY="
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
if not defined PY (
    py -3.14 --version >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%X in ('py -3.14 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%X"
    )
)
if not defined PY if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo  [ERROR] No Python found. Run install.bat first.
    if not defined HIVEMIND_NO_PAUSE (
        echo  Press any key to continue... & pause >nul
    )
    exit /b 1
)

where uvx >nul 2>&1
if errorlevel 1 (
    if exist "%USERPROFILE%\.local\bin\uvx.exe" set "PATH=%PATH%;%USERPROFILE%\.local\bin"
    if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uvx.exe" set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links"
)

set "MODEL="
set "DOTTS=0"
set "DOWOOF=0"
set "DOBOTH=0"
set "DOHELP=0"
for %%A in (%*) do (
    if /i "%%~A"=="--help" set "DOHELP=1"
    if /i "%%~A"=="-h"     set "DOHELP=1"
    if /i "%%~A"=="--tts"  set "DOTTS=1"
    if /i "%%~A"=="--woof" set "DOWOOF=1"
    if /i "%%~A"=="--both" (set "DOTTS=1" & set "DOBOTH=1")
    if /i not "%%~A"=="--tts" if /i not "%%~A"=="--both" if /i not "%%~A"=="--woof" if /i not "%%~A"=="--help" if /i not "%%~A"=="-h" set "MODEL=%%~A"
)
if "%DOHELP%"=="1" goto show_help
if "%MODEL%"=="" set "MODEL=parakeet-q4k"

set "DOSTT=1"
if "%DOBOTH%"=="0" (
    if "%DOTTS%"=="1" set "DOSTT=0"
    if "%DOWOOF%"=="1" set "DOSTT=0"
)
if not "%~1"=="" if /i not "%~1"=="--tts" if /i not "%~1"=="--woof" if /i not "%~1"=="--both" set "DOSTT=1"
for %%A in (%*) do (
    if /i not "%%~A"=="--tts" if /i not "%%~A"=="--both" if /i not "%%~A"=="--woof" if /i not "%%~A"=="--help" if /i not "%%~A"=="-h" set "DOSTT=1"
)

echo.
echo  +-----------------------------------------------------------+
echo   HiveMind voice setup
echo  +-----------------------------------------------------------+
echo.

if "%DOSTT%"=="1" goto do_stt
echo  [1/2] Speech to text - skipped
echo.
goto stt_done

:do_stt
echo  [1/2] Speech to text - engine + model: %MODEL%
echo.
"%PY%" deploy\fetch_whisper.py --model "%MODEL%"
if errorlevel 1 goto stt_fail
echo.

:stt_done
if "%DOTTS%"=="1" goto do_tts
if "%DOWOOF%"=="1" goto do_woof
echo  [2/2] Text to speech - skipped
echo.
goto summary

:do_woof
echo  [2/2] Text to speech - Underdog Woof ^(CPU NumPy, ~41 MB, no HF login^)
echo.
"%PY%" deploy\fetch_woof.py
if errorlevel 1 echo   [WARN] Underdog Woof download failed - retry via Settings -^> Voice.
echo.
goto summary

:do_tts
echo  [2/2] Text to speech - Pocket TTS, voice anna
echo.
echo   Pre-installing the isolated environment - PyTorch CPU, about 200 MB...
uvx --python 3.12 --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match --from pocket-tts python -c "import pocket_tts" >nul 2>&1
if errorlevel 1 goto tts_env_fail
echo   [OK] Environment ready.
echo.
echo   Fetching the gated model (one-time Hugging Face login if not done)...
uvx --python 3.12 --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match pocket-tts generate --text "Voice setup complete." --output-path "%TEMP%\hivemind_tts_check.wav" >nul 2>&1
if errorlevel 1 goto tts_model_fail
echo   [OK] Model ready.
del "%TEMP%\hivemind_tts_check.wav" >nul 2>&1
echo.
if "%DOWOOF%"=="1" goto do_woof
goto summary

:tts_env_fail
echo   [WARN] Could not prepare the Pocket TTS environment.
echo          Needs "uv" on PATH. You can retry later from the UI.
echo.
if "%DOWOOF%"=="1" goto do_woof
goto summary

:tts_model_fail
echo   [WARN] The model could not be fetched.
echo.
echo          Pocket TTS is gated. Three steps, once:
echo            1. Accept the terms:
echo               https://huggingface.co/kyutai/pocket-tts
echo            2. Create a Read token:
echo               https://huggingface.co/settings/tokens
echo            3. Log in here - the token input stays hidden.
echo.
echo          The Hugging Face CLI stores the token in
echo          %%USERPROFILE%%\.cache\huggingface\token
echo          HiveMind itself never creates or stores a token.
echo.
choice /c YN /n /m "Log in to Hugging Face now? (token input hidden) [Y/N] "
if errorlevel 2 goto tts_model_skip
echo.
echo   The token is read hidden and stored by the Hugging Face CLI in
echo   %%USERPROFILE%%\.cache\huggingface\token - never in this batch or a log.
uvx --from huggingface_hub hf auth login
if errorlevel 1 goto tts_model_skip
echo.
echo   Retrying the model download...
uvx --python 3.12 --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match pocket-tts generate --text "Voice setup complete." --output-path "%TEMP%\hivemind_tts_check.wav" >nul 2>&1
if errorlevel 1 goto tts_model_skip
del "%TEMP%\hivemind_tts_check.wav" >nul 2>&1
echo   [OK] Model ready.
echo.
if "%DOWOOF%"=="1" goto do_woof
goto summary

:tts_model_skip
echo.
echo          Skipped - the first request in the UI fetches the model once the
echo          token is in place.
echo.
if "%DOWOOF%"=="1" goto do_woof
goto summary

:stt_fail
echo.
echo  [ERROR] Speech-to-text download failed. Check the network, or use the
echo          Download button in Settings -^> Voice.
echo.
if not defined HIVEMIND_NO_PAUSE (
    echo  Press any key to continue...
    pause >nul
)
exit /b 1

:show_help
echo.
echo  Usage: deploy\setup_voice.bat [model] [--tts^|--woof^|--both]
echo    model   STT model: parakeet-q4k (default^), base, tiny, small
echo    --tts   Pocket TTS setup only
echo    --woof  Underdog Woof CPU TTS only
echo    --both  STT + Pocket TTS
echo.
exit /b 0

:summary
echo  ------------------------------------------------------------
echo   Next: Settings -^> Voice
echo     - Speech to text: switch on, then "Record 3 s" to test it.
echo     - Text to speech: switch on, pick a voice, "Play sample".
echo   Telegram voice notes: Settings -^> Telegram Gateway
echo     - greyed out until the matching feature above is on.
echo  ------------------------------------------------------------
echo.
if not defined HIVEMIND_NO_PAUSE (
    echo  Press any key to continue...
    pause >nul
)
exit /b 0
