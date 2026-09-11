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

REM ---- Catalog: single source of truth is fetch_models.py SPECS ----
echo  Download catalog (existing models stay untouched and get registered):
echo.
"%PY%" deploy\fetch_models.py --print-catalog
echo.
echo    LFM2.5 2.6B also downloads the DSpark spec-dec drafter.
echo    Gemma-4 E4B/E2B QAT also download their MTP drafter.
echo.
echo  D = download all missing, S = select single models,
echo  C = add own model with config, R = register folder only (no download),
echo  A = abort.
echo.
choice /c DSCRA /m "Your choice"
if errorlevel 5 exit /b 0
if errorlevel 4 goto import_only
if errorlevel 3 goto custom_add
if errorlevel 2 goto select_single

echo.
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --only-missing
goto done

:select_single
echo.
echo  Single-model download - enter numbers, comma-separated, e.g. 1,4
echo  (empty input = register the folder only, no download):
echo.
set "MODELS_SEL="
set /p "MODELS_SEL=Numbers: "
if "%MODELS_SEL%"=="" goto import_only
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
