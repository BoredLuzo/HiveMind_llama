@echo off
setlocal EnableExtensions
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
echo  Press any key to continue... & pause >nul
REM FINAL SELF-SWAP (2026-10-03 review): updater.py stages the incoming
REM update.bat as .update_new.bat; ONE physical line here - cmd parses it
REM whole before executing - swaps it in and exits. NOTHING may follow
REM this line: cmd resumes at a byte offset of the OLD file after the
REM copy, so trailing lines would run stale bytes (audit 2026-11-07).
REM Only a SUCCESSFUL update may replace update.bat (the old batch exited
REM before its swap on every error path).
if not "%UPD_RC%"=="0" exit /b %UPD_RC%
if exist "%~dp0.update_new.bat" (copy /y "%~dp0.update_new.bat" "%~dp0update.bat" >nul & del "%~dp0.update_new.bat" >nul 2>&1 & exit /b %UPD_RC%) else exit /b %UPD_RC%
