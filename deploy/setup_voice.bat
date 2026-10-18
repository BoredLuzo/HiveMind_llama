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
REM  Usage:  deploy\setup_voice.bat [model] [--tts|--both]
REM          deploy\setup_voice.bat base --tts
REM  Models: parakeet-q4k (default, 396 MB) | base (141 MB) | tiny (74 MB) | small (465 MB)
REM
REM  NOTE: label based on purpose - parenthesised if-blocks in cmd.exe choke on
REM  text that itself contains ")" (the first version printed half a line and
REM  silently skipped the TTS part).
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "MODEL="
set "DOTTS=0"
for %%A in (%*) do (
    if /i "%%~A"=="--tts"  set "DOTTS=1"
    if /i "%%~A"=="--both" set "DOTTS=1"
    if /i not "%%~A"=="--tts" if /i not "%%~A"=="--both" set "MODEL=%%~A"
)
if "%MODEL%"=="" set "MODEL=parakeet-q4k"

set "DOSTT=1"
if /i "%~1"=="--tts" set "DOSTT=0"

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
echo  [2/2] Text to speech - skipped
echo.
goto summary

:do_tts
echo  [2/2] Text to speech - Pocket TTS, voice cosette
echo.
echo   Pre-installing the isolated environment - PyTorch CPU, about 200 MB...
uvx --python 3.12 --index https://download.pytorch.org/whl/cpu --from pocket-tts python -c "import pocket_tts" >nul 2>&1
if errorlevel 1 goto tts_env_fail
echo   [OK] Environment ready.
echo.
echo   Fetching the model - gated, needs a one-time "hf auth login"...
uvx --python 3.12 --index https://download.pytorch.org/whl/cpu pocket-tts generate --text "Voice setup complete." --output-path "%TEMP%\hivemind_tts_check.wav" >nul 2>&1
if errorlevel 1 goto tts_model_fail
echo   [OK] Model ready.
del "%TEMP%\hivemind_tts_check.wav" >nul 2>&1
echo.
goto summary

:tts_env_fail
echo   [WARN] Could not prepare the Pocket TTS environment.
echo          Needs "uv" on PATH. You can retry later from the UI.
echo.
goto summary

:tts_model_fail
echo   [WARN] The model could not be fetched.
echo          Most likely the Hugging Face terms are not accepted yet:
echo            1. hf auth login          (needs huggingface_hub, see below)
echo            2. open https://huggingface.co/kyutai/pocket-tts and accept
echo          Full guide: docs\huggingface_token.md
echo          Token in a shell with the hub CLI:
echo            uvx --from huggingface_hub hf auth login
echo          Afterwards the first request in the UI fetches the model.
echo.
goto summary

:stt_fail
echo.
echo  [ERROR] Speech-to-text download failed. Check the network, or use the
echo          Download button in Settings -^> Voice.
echo.
pause >nul
exit /b 1

:summary
echo  ------------------------------------------------------------
echo   Next: Settings -^> Voice
echo     - Speech to text: switch on, then "Record 3 s" to test it.
echo     - Text to speech: switch on, pick a voice, "Play sample".
echo   Telegram voice notes: Settings -^> Telegram Gateway
echo     - greyed out until the matching feature above is on.
echo  ------------------------------------------------------------
echo.
pause >nul
exit /b 0
