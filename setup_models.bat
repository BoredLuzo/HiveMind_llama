@echo off
setlocal EnableDelayedExpansion
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

REM ---- Models folder: arg > env > interactive prompt > default <root>\models ----
REM This is the ONLY place the folder is asked. install.bat calls this script
REM without arguments, so the prompt appears exactly once during an install.
set "MODELS_DIR=%~1"
if "%MODELS_DIR%"=="" set "MODELS_DIR=%HIVEMIND_MODELS_DIR%"
if "%MODELS_DIR%"=="" (
    echo  Where should models be downloaded / searched?
    echo  Existing GGUFs in that folder get registered automatically.
    echo.
    set "MODELS_DIR="
    set /p "MODELS_DIR=Folder [Enter = %~dp0models]: "
    if "!MODELS_DIR!"=="" set "MODELS_DIR=%~dp0models"
)

REM strip any quotes from pasted paths (delayed expansion keeps it safe)
set MODELS_DIR=!MODELS_DIR:"=!

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

:menu
REM ---- Catalog: single source of truth is fetch_models.py SPECS ----
echo.
echo  WHAT CAN BE INSTALLED
echo  ====================================================================
echo.
"%PY%" deploy\fetch_models.py --print-essential
echo  HOW TO PROCEED
echo  ====================================================================
echo.
echo    [E]  essential set - the four models above (recommended start)
echo    [S]  pick single models from the full catalog
echo    [D]  everything missing from the catalog
echo    [C]  add your own model, with a config
echo    [R]  register an existing models folder, no download
echo    [A]  abort
echo.
echo    Vision projectors and MTP drafters are downloaded with their model.
echo.
choice /c DESCRA /m "Your choice"
if errorlevel 6 exit /b 0
if errorlevel 5 goto import_only
if errorlevel 4 goto custom_add
if errorlevel 3 goto select_single
if errorlevel 2 goto essential_set

echo.
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --only-missing
goto more_models

:essential_set
echo.
echo  Essential set only. Answering "n" to a model skips it completely.
echo.
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --only-missing --essential
goto more_models

:select_single
echo.
echo  SINGLE MODELS
echo  ====================================================================
echo.
"%PY%" deploy\fetch_models.py --print-catalog
echo  Enter the numbers, comma-separated (e.g. 1,4).
echo  Empty input = register the folder only, no download.
echo.
set "MODELS_SEL="
set /p "MODELS_SEL=Numbers: "
if "%MODELS_SEL%"=="" goto import_only
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --only "%MODELS_SEL%"
goto more_models

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
goto more_models

:import_only
echo.
REM Scan-only: auto-detect own models and register them in models.json.
REM No download, no network access needed.
"%PY%" deploy\fetch_models.py --models-dir "%MODELS_DIR%" --scan-only

:more_models
REM Step finished. Ask explicitly: more models, or move on. Two named keys beat
REM a [Y/N] that does not say what "no" does.
echo.
echo  --------------------------------------------------------------------
echo   Step finished. What now?
echo.
echo     [M]  download more models (back to the list)
echo     [P]  proceed (finish this step)
echo  --------------------------------------------------------------------
echo.
choice /c MP /n /m "Your choice [M/P] "
if not errorlevel 2 goto menu
goto done

:done
echo.
echo  Done. models.json was populated from the models folder.
echo  Then start HiveMind with start_hivemind.bat
echo.
echo  Press any key to continue...
pause >nul
