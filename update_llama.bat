@echo off
setlocal
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - llama.cpp UPDATE                              ^|
echo  ^|    Download the latest llama.cpp build                      ^|
echo  +=============================================================+
echo.
echo  What this does:
echo    - Detects your GPU (NVIDIA vs. AMD/Intel) for the right default.
echo    - Downloads the newest llama.cpp build for the selected backend
echo      (CUDA for NVIDIA, Vulkan otherwise) and installs it into \llama.
echo    - Old builds are kept - delete old llama-bXXXX-* folders
echo      manually to free disk space.
echo.

REM Find Python (venv preferred, then system)
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

REM Default backend from settings.json (gpu_backend), fallback: vulkan
set "BACKEND=vulkan"
if exist "settings.json" (
    for /f "usebackq delims=" %%B in (`powershell -NoProfile -Command "$j = Get-Content 'settings.json' -Raw | ConvertFrom-Json; if ($j.gpu_backend) { $j.gpu_backend }"`) do set "BACKEND=%%B"
)
if not "%BACKEND%"=="cuda" set "BACKEND=vulkan"

REM Detect NVIDIA GPU for a better default
set "HM_NVIDIA="
nvidia-smi --query-gpu=name --format=csv,noheader >nul 2>&1
if not errorlevel 1 set "HM_NVIDIA=1"

if defined HM_NVIDIA (
    echo  NVIDIA GPU detected.
    choice /c CV /m "Update [C]uda (recommended) / [V]ulkan backend? (current: %BACKEND%)"
    set "BACKEND=cuda"
    if errorlevel 2 set "BACKEND=vulkan"
) else (
    echo  No NVIDIA GPU detected - Vulkan recommended for AMD/Intel.
    choice /c VC /m "Update [V]ulkan (recommended) / [C]uda backend?"
    set "BACKEND=vulkan"
    if errorlevel 2 set "BACKEND=cuda"
)

echo.
echo  [..] Updating llama.cpp (%BACKEND%)...
echo   Target: %~dp0llama\
"%PY%" deploy\fetch_llamacpp.py --backend %BACKEND% --force
if errorlevel 1 (
    echo.
    echo  [ERROR] Update failed. See the message above.
    echo.
    echo  Press any key to continue...
    pause >nul
    exit /b 1
)

echo.
echo  [OK] Done. Old builds stay in llama\ - delete old llama-bXXXX-*
echo       folders manually to free disk space.
echo.
echo  Press any key to continue...
pause >nul