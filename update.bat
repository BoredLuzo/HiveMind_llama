@echo off
setlocal EnableExtensions
set "HIVEMIND_CALL_CWD=%CD%"
cd /d "%~dp0"
REM HiveMind updater - thin shim (2026-11-06, split 5; ladder fixed 2026-11-07).
REM The logic lives in deploy\updater.py: a real Python script, testable
REM against fake trees, no cmd/PowerShell quoting hazards (an apostrophe in
REM the install path used to break every inline PS call). This file only
REM resolves Python the same way start_hivemind.bat does and hands over.
echo.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not "%PY%"=="python" goto py_found
REM py -3.14 must be resolved to a REAL path: "%PY%" below is quoted, and
REM a quoted "py -3.14" is one nonexistent filename (cmd rc 9009).
py -3.14 --version >nul 2>&1
if errorlevel 1 goto py_known_paths
for /f "delims=" %%X in ('py -3.14 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%X"
:py_known_paths
if not "%PY%"=="python" goto py_found
if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
if "%PY%"=="python" if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PY=%LocalAppData%\Programs\Python\Python314\python.exe"
:py_found
if "%PY%"=="python" (
    echo  [ERROR] No Python found. Run install.bat first, then retry the update.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
"%PY%" "%~dp0deploy\updater.py" %*
set "UPD_RC=%ERRORLEVEL%"
if not "%UPD_RC%"=="0" if not defined HIVEMIND_NO_PAUSE echo  Press any key to continue...
if not "%UPD_RC%"=="0" if not defined HIVEMIND_NO_PAUSE pause >nul
if not "%UPD_RC%"=="0" exit /b %UPD_RC%
REM FINAL SELF-SWAP (2026-10-03 review, post-swap pause 2026-11-17):
REM updater.py stages the incoming update.bat as .update_new.bat; ONE
REM physical line here swaps it in, deletes the staging file, optionally
REM pauses (skipped when HIVEMIND_NO_PAUSE=1), and exits. NOTHING may follow
REM this line: cmd parses the entire line before running any part of it, so
REM closing the window with [X] at pause still leaves the new update.bat on disk.
(if exist "%~dp0.update_new.bat" copy /y "%~dp0.update_new.bat" "%~dp0update.bat" >nul) & (if exist "%~dp0.update_new.bat" del "%~dp0.update_new.bat" >nul 2>&1) & (if not defined HIVEMIND_NO_PAUSE echo  Press any key to continue...) & (if not defined HIVEMIND_NO_PAUSE pause >nul) & exit /b %UPD_RC%
