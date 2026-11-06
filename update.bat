@echo off
setlocal EnableExtensions
cd /d "%~dp0"
REM HiveMind updater - thin shim (2026-11-06, split 5).
REM The logic lives in deploy\updater.py: a real Python script, testable
REM against fake trees, no cmd/PowerShell quoting hazards (an apostrophe in
REM the install path used to break every inline PS call). This file only
REM resolves Python the same way start_hivemind.bat does and hands over.
echo.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%~dp0.venv\Scripts\python.exe" (
    where py >nul 2>&1 && set "PY=py -3.14"
)
if not exist "%~dp0.venv\Scripts\python.exe" if exist "C:\Program Files\Python314\python.exe" set "PY=C:\Program Files\Python314\python.exe"
"%PY%" "%~dp0deploy\updater.py" %*
set "UPD_RC=%ERRORLEVEL%"
REM FINAL SELF-SWAP (2026-10-03 review): updater.py stages the incoming
REM update.bat as .update_new.bat; ONE physical line here - cmd parses it
REM whole before executing - swaps it in. Nothing may follow this line.
if exist "%~dp0.update_new.bat" (copy /y "%~dp0.update_new.bat" "%~dp0update.bat" >nul & del "%~dp0.update_new.bat" >nul 2>&1)
echo  Press any key to continue... & pause >nul
exit /b %UPD_RC%
