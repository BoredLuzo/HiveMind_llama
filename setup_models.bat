@echo off
setlocal
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - MODEL SETUP                                   ^|
echo  ^|    Download / register GGUF models                          ^|
echo  +=============================================================+
echo.
echo  Registers and downloads the GGUF models used by HiveMind.
echo  Target folder: argument 1 ^> env HIVEMIND_MODELS_DIR ^> default \models
echo.

REM ---- Models folder: arg > env > default <root>\models ----
set "MODELS_DIR=%~1"
if "%MODELS_DIR%"=="" set "MODELS_DIR=%HIVEMIND_MODELS_DIR%"
if "%MODELS_DIR%"=="" set "MODELS_DIR=%~dp0models"

echo  Target folder: %MODELS_DIR%
if not exist "%MODELS_DIR%" (
    echo  Folder does not exist - it will be created.
    mkdir "%MODELS_DIR%" 2>nul
)
echo.

REM ---- Find Python (venv preferred, then system) ----
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo.
        echo  [ERROR] Python not found. Please run install.bat first.
        echo.
        echo  Press any key to continue... & pause >nul & exit /b 1
    )
)

REM ---- GGUF autodetect: models already present? ----
set "HAS_GGUF="
for /f %%i in ('dir /b /s "%MODELS_DIR%\*.gguf" 2^>nul ^| find /c /v ""') do set HAS_GGUF=%%i
if defined HAS_GGUF if not "%HAS_GGUF%"=="0" (
    echo  [%HAS_GGUF%] GGUF files found in the folder.
    echo  Missing recommended models will be downloaded,
    echo  existing ones stay untouched and get registered.
    echo.
)

echo  The following models can be loaded:
echo    1. Gemma-4 E4B-IT          Q4_K_M     ~3 GB    All-rounder/Vision
echo    2. Qwen3.6 35B A3B UD      Q4_K_XL   ~20 GB    Coder/Planner MoE
echo    3. Qwen3.5 4B UD           Q4_K_XL    ~3 GB    Analyst/Critic/Speed
echo    4. Qwen3.5 9B UD           Q4_K_XL    ~6 GB    Direct/Duo-Coder
echo    5. Qwen3.5 2B              Q4_K_M     ~1.3 GB  Refiner
echo    6. LFM2.5 2.6B             Q4_K_M     ~2 GB    Subagent/Judge (+DSpark drafter)
echo    7. Qwen3.5 0.8B UD         Q4_K_XL    ~0.6 GB  Subagent ladder
echo    8. Hermes3.6 V12 MTP-APEX-Compact  APEX-Compact ~17 GB   Coder/Hermes (MoE+MTP)
echo.
echo    LFM2.5 2.6B automatically also downloads the DSpark spec-dec drafter.
echo.
choice /c DCRA /m "[D]ownload / [C]ustom model add / [R]egister own folder only / [A]bort"
if errorlevel 4 exit /b 0
if errorlevel 3 goto import_only
if errorlevel 2 goto custom_add

echo.
REM Single-select mode: pick specific models by number (comma-separated).
choice /c AS /m "[A]ll recommended models / [S]elect single models"
if errorlevel 2 goto select_single

echo.
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --only-missing
goto done

:select_single
echo.
echo  Single-model download - enter numbers, comma-separated, e.g. 1,4:
echo    1. gemma-4:e4b-it        All-rounder/Vision
echo    2. qwen3.6:35b-a3b-ud    Coder/Planner MoE
echo    3. lfm2.5:2.6b           Subagent/Judge (+DSpark drafter)
echo    4. qwen3.5:0.8b-ud       Subagent ladder
echo    5. qwen3.5:2b            Refiner
echo    6. qwen3.5:4b-ud         Analyst/Critic/Speed
echo    7. qwen3.5:9b-ud         Direct/Duo-Coder
echo    8. hermes3.6:35b-a3b-uncensored-genesis-v12-mtp-apex-compact  Coder/Hermes (MoE+MTP)
echo    9. tiel-coder:35b-a3b-mtp-compact   Tiel-Coder MTP-Compact (BoredLuzo, MoE+MTP)
echo   10. tiel-coder:35b-a3b-mtp-apex      Tiel-Coder MTP-APEX (BoredLuzo, MoE+MTP)
echo.
set "MODELS_SEL="
set /p "MODELS_SEL=Numbers: "
if "%MODELS_SEL%"=="" goto done
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --only "%MODELS_SEL%"
goto done

:custom_add
echo.
echo  --------------------------------------------------------------
echo   Custom model: interactive assistant for adding your own model
echo   WITH a config (capabilities, context, launch settings).
echo   Writes: models.json + model_configs\models\^<name^>.json
echo   Optional: assign it to an agent role in settings.json.
echo  --------------------------------------------------------------
echo.
"%PY%" deploy\add_model.py "%MODELS_DIR%"
goto done

:import_only
echo.
REM Scan-only: auto-detect own models and register them in models.json.
REM No download, no network access needed.
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --scan-only

:done
echo.
echo  Done. models.json was populated from the models folder.
echo  Then start HiveMind with start_hivemind.bat
echo.
echo  Press any key to continue...
pause >nul