@echo off
setlocal
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - DESKTOP SHORTCUT                              ^|
echo  ^|    Creates a Desktop shortcut for start_hivemind.bat        ^|
echo  +=============================================================+
echo.

REM ---- Optional: shortcut name (without .lnk), default "HiveMind" ----
set "SC_NAME=HiveMind"
if not "%~1"=="" set "SC_NAME=%~1"

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

echo  Target folder : %HM_DESKTOP%
echo  Shortcut      : %HM_LNK%
echo  Icon          : %HM_ICON%
echo.

powershell -NoProfile -ExecutionPolicy ByPass -Command "$sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut($env:HM_LNK); $s.TargetPath = $env:HM_TARGET; $s.WorkingDirectory = $env:HM_WORKDIR; if ($env:HM_ICON_ARG) { $s.IconLocation = $env:HM_ICON_ARG }; $s.Description = 'HiveMind - local multi-agent AI coding assistant'; $s.Save()"
if errorlevel 1 (
    echo.
    echo  [ERROR] Could not create the desktop shortcut.
    echo.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

echo  [OK] Desktop shortcut created.
echo       Double-click "%SC_NAME%" to start HiveMind.
echo.
echo  Press any key to continue...
pause >nul
exit /b 0
