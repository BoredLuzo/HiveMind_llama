@echo off
setlocal
cd /d "%~dp0"

REM --quiet: idempotent refresh, no banner, no pauses (ICON-FIX 2026-11-01:
REM update.bat calls this after every update so the desktop shortcut always
REM exists and always carries the icon - on installs from older zips the
REM .lnk had no IconLocation and the icon stayed missing forever).
set "QUIET="
if /I "%~1"=="--quiet" set "QUIET=1"

if defined QUIET goto skip_banner

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - DESKTOP SHORTCUT                              ^|
echo  ^|    Creates a Desktop shortcut for start_hivemind.bat        ^|
echo  +=============================================================+
echo.
:skip_banner

REM ---- Optional: shortcut name (without .lnk), default "HiveMind" ----
REM QUIET-FLAG (audit 2026-11-03): update.bat passes --quiet; it used to
REM become the shortcut NAME (stray "--quiet.lnk", no icon refresh).
set "SC_NAME=HiveMind"
if /i "%~1"=="--quiet" goto sc_name_done
if not "%~1"=="" set "SC_NAME=%~1"
:sc_name_done

REM ---- Real Desktop folder (OneDrive-aware) ----
set "HM_DESKTOP="
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "$d=[Environment]::GetFolderPath('Desktop'); if(-not $d){$d=Join-Path $env:USERPROFILE 'Desktop'}; Write-Output $d"`) do set "HM_DESKTOP=%%D"
if not defined HM_DESKTOP set "HM_DESKTOP=%USERPROFILE%\Desktop"
if not exist "%HM_DESKTOP%\" mkdir "%HM_DESKTOP%" 2>nul

REM ---- Paths (passed to PowerShell via env vars: spaces/parentheses safe) ----
set "HM_TARGET=%~dp0start_hivemind.bat"
set "HM_WORKDIR=%~dp0"
set "HM_LNK=%HM_DESKTOP%\%SC_NAME%.lnk"

REM ---- Icon (optional; .ico only, e.g. static\favicon.ico) ----
set "HM_ICON=%~dp0static\favicon.ico"
set "HM_ICON_ARG="
if not exist "%HM_ICON%" goto icon_default
set "HM_ICON_ARG=%HM_ICON%,0"
goto icon_set
:icon_default
echo  [WARN] Icon not found: %HM_ICON%
echo         Using the default icon instead.
:icon_set

if defined QUIET goto write_shortcut
echo  Target folder : %HM_DESKTOP%
echo  Shortcut      : %HM_LNK%
echo  Icon          : %HM_ICON%
echo.
:write_shortcut

powershell -NoProfile -ExecutionPolicy ByPass -Command "$sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut($env:HM_LNK); $s.TargetPath = $env:HM_TARGET; $s.WorkingDirectory = $env:HM_WORKDIR; if ($env:HM_ICON_ARG) { $s.IconLocation = $env:HM_ICON_ARG }; $s.Description = 'HiveMind - local multi-agent AI coding assistant'; $s.Save()"
if errorlevel 1 (
    if defined QUIET exit /b 1
    echo.
    echo  [ERROR] Could not create the desktop shortcut.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM ICON-FIX: refresh the shell icon cache so a replaced .ico shows at
REM once instead of a blank/gear placeholder (regularly seen on laptops).
tasklist /FI "IMAGENAME eq explorer.exe" 2>nul | find /I "explorer.exe" >nul
if not errorlevel 1 ie4uinit.exe -show >nul 2>&1

if defined QUIET exit /b 0

echo  [OK] Desktop shortcut created.
echo       Double-click "%SC_NAME%" to start HiveMind.
echo.
echo  Press any key to continue...
pause >nul
exit /b 0
