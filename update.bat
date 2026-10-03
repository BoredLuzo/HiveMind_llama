@echo off
setlocal EnableExtensions
cd /d "%~dp0"
REM BASE = this folder WITHOUT the trailing backslash (a "%~dp0" inside a
REM quoted string breaks robocopy args: the final backslash escapes the closing quote)
set "BASE=%~dp0"
set "BASE=%BASE:~0,-1%"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - UPDATE                                        ^|
echo  ^|    Update this installation from the latest GitHub release  ^|
echo  +=============================================================+
echo.
echo  What this does:
echo    - Checks GitHub for the latest HiveMind release
echo    - Backs up the current code to update_backup_(version)\
echo    - Shows warnings from the release notes before applying
echo    - Applies the new build over this folder
echo  Your settings.json, models.json, .venv, llama\, logs\ and
echo  sessions\ are NOT touched. New settings keys pick up their
echo  defaults automatically on the next start.
echo.

REM ── Config ────────────────────────────────────────────────────────────────
set "REPO=BoredLuzo/HiveMind_llama"
set "TMPDIR=%~dp0_update_tmp"

REM ── 0. curl present? (ships with Windows 10 1803+) ────────────────────────
where curl >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] curl.exe not found. HiveMind updates need Windows 10 1803+
    echo         or a curl.exe on PATH.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM ── 1. Is an instance running on the configured port? ─────────────────────
REM Port resolution mirrors run.py: HIVEMIND_PORT env > settings.json > 8001.
REM The check uses Get-NetTCPConnection (locale-proof: netstat prints
REM netstat "LISTENING" check is localized (German netstat prints a different word).
set "HM_PORT=8001"
if defined HIVEMIND_PORT set "HM_PORT=%HIVEMIND_PORT%"
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$j = Get-Content 'settings.json' -Raw -ErrorAction SilentlyContinue; $e = $env:HIVEMIND_PORT; if ($e) { $e } elseif ($j -and $j.server_port) { $j.server_port } else { '8001' }"`) do set "HM_PORT=%%P"
if not defined HM_PORT set "HM_PORT=8001"

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %HM_PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo  [ERROR] A HiveMind instance is listening on port %HM_PORT%.
    echo         Stop it first: close the HiveMind console window, then
    echo         run this update again.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo  [OK] No running instance on port %HM_PORT%.

REM ── 2. Current version (server.py) ────────────────────────────────────────
set "CURRENT="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "(Select-String -Path server.py -Pattern 'HIVEMIND_VERSION\s*=\s*.([0-9.]+)').Matches[0].Groups[1].Value"`) do set "CURRENT=%%V"
if not defined CURRENT (
    echo  [ERROR] Could not read HIVEMIND_VERSION from server.py.
    echo         Is this a HiveMind installation folder?
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo  Installed version : %CURRENT%

REM ── 3. Latest release from GitHub ─────────────────────────────────────────
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
mkdir "%TMPDIR%" 2>nul
curl -sS -L "https://api.github.com/repos/%REPO%/releases/latest" -o "%TMPDIR%\release.json"
if errorlevel 1 (
    echo  [ERROR] Could not reach GitHub. Check your internet connection
    echo         and try again.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

set "TAG="
set "ASSET_URL="
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command "$r = Get-Content '%TMPDIR%\release.json' -Raw | ConvertFrom-Json; $a = $r.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1; Write-Output ($r.tag_name + '|' + $a.browser_download_url)"`) do set "LINE=%%L"
for /f "tokens=1,2 delims=|" %%T in ("%LINE%") do (
    set "TAG=%%T"
    set "ASSET_URL=%%U"
)
if defined TAG if defined ASSET_URL goto tag_ok
echo  [ERROR] GitHub returned no release - rate limit or private repo?
echo         Try again in a minute, or download the zip manually from
echo         https://github.com/%REPO%/releases/latest
echo  Press any key to continue... & pause >nul & exit /b 1
:tag_ok

REM ── 4. Compare versions ───────────────────────────────────────────────────
if not "%TAG:~0,1%"=="v" (
    echo  [ERROR] Unexpected release tag "%TAG%" - aborting. Download the
    echo         zip manually from https://github.com/%REPO%/releases/latest
    echo  Press any key to continue... & pause >nul & exit /b 1
)
set "TAGNUM=%TAG:v=%"
echo  Latest release    : %TAGNUM%
REM Numeric triple compare: string equality would apply a DOWNGRADE when the
REM installed version is newer (dev checkouts, hotfix builds).
set "VCMP="
for /f "usebackq delims=" %%C in (`powershell -NoProfile -Command "function V([string]$v) { @($v -split '\.') + 0,0,0 | Select-Object -First 3 | ForEach-Object { [int]$_ } }; $a = V '%CURRENT%'; $b = V '%TAGNUM%'; if ($b[0] -gt $a[0] -or ($b[0] -eq $a[0] -and $b[1] -gt $a[1]) -or ($b[0] -eq $a[0] -and $b[1] -eq $a[1] -and $b[2] -gt $a[2])) { 'newer' } elseif ($b[0] -eq $a[0] -and $b[1] -eq $a[1] -and $b[2] -eq $a[2]) { 'same' } else { 'older' }"`) do set "VCMP=%%C"
if "%VCMP%"=="same" (
    echo.
    echo  [OK] Already up to date: %CURRENT%
    if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
    echo  Press any key to continue... & pause >nul & exit /b 0
)
if "%VCMP%"=="older" (
    echo.
    echo  [OK] Installed %CURRENT% is NEWER than release %TAGNUM% - nothing to do.
    echo         (Not downgrading.)
    if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
    echo  Press any key to continue... & pause >nul & exit /b 0
)

REM ── 4a. Render the release notes (markdown-light -> plain console text) ──
powershell -NoProfile -Command "$r = Get-Content '%TMPDIR%\release.json' -Raw | ConvertFrom-Json; $o = foreach ($l in ($r.body -split ([char]10))) { $t = $l.TrimEnd() -replace '\*\*','' -replace '`','' -replace '^#{1,6}\s*','== '; $t }; $o | Set-Content '%TMPDIR%\notes.txt'"
echo.
echo  +-------------------------------------------------------------+
echo  ^|  RELEASE NOTES - %TAG%                     ^|
echo  +-------------------------------------------------------------+
type "%TMPDIR%\notes.txt"
echo  +-------------------------------------------------------------+
echo.

REM ── 4b. Warnings from the release notes ───────────────────────────────────
REM Anything the release author flags (warning / breaking / known issue /
REM migration / attention / caution) is shown BEFORE anything is changed.
del "%TMPDIR%\warnings.txt" 2>nul
powershell -NoProfile -Command "$r = Get-Content '%TMPDIR%\release.json' -Raw | ConvertFrom-Json; $w = $r.body -split ([char]10) | Where-Object { $_ -match '(?i)warning|breaking|known issue|migrat|attention|caution' } | Select-Object -First 5; if ($w) { $w | Set-Content '%TMPDIR%\warnings.txt' }"
if exist "%TMPDIR%\warnings.txt" (
    echo.
    echo  +-------------------------------------------------------------+
    echo  ^|  THE RELEASE NOTES WARN ABOUT THIS UPDATE:                  ^|
    echo  +-------------------------------------------------------------+
    type "%TMPDIR%\warnings.txt"
    echo  +-------------------------------------------------------------+
    set "CONT="
    set /p "CONT=Continue anyway? [Y/n]: "
    if /I "%CONT%"=="n" (
        echo  Update cancelled. Nothing was changed.
        if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
        echo  Press any key to continue... & pause >nul & exit /b 0
    )
)

REM ── 5. Backup the current code (user data is excluded) ────────────────────
set "BK=update_backup_%TAGNUM%"
echo  Backing up code   : %BK%\
REM /R:1 /W:1: a single locked file must not hang the update for hours
REM (robocopy defaults: 1,000,000 retries x 30s). models excluded: GGUFs
REM can live here in some setups - gigabytes nobody wants in a backup.
REM old update_backup_* excluded: backups must not nest.
robocopy "%BASE%" "%BASE%\%BK%" /E /R:1 /W:1 /NFL /NDL /NJH /NP /NJS /XD .venv llama logs models sessions learning_logs __pycache__ _update_tmp update_backup_*
if errorlevel 8 (
    echo  [ERROR] Backup failed - nothing was changed.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM ── 6. Download the release zip ───────────────────────────────────────────
echo  Downloading       : %ASSET_URL%
curl -sS -L -o "%TMPDIR%\release.zip" "%ASSET_URL%"
if errorlevel 1 (
    echo  [ERROR] Download failed. Nothing was changed
    echo         backup is in %BK%
    echo  Press any key to continue... & pause >nul & exit /b 1
)
if not exist "%TMPDIR%\release.zip" (
    echo  [ERROR] Download produced no file. Nothing was changed.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM ── 7. Extract (zip carries a version root folder) ────────────────────────
powershell -NoProfile -Command "Expand-Archive -Path '%TMPDIR%\release.zip' -DestinationPath '%TMPDIR%\extracted' -Force"
if errorlevel 1 (
    echo  [ERROR] Could not extract the zip. Nothing was changed
    echo         backup is in %BK%
    echo  Press any key to continue... & pause >nul & exit /b 1
)
set "EXROOT="
for /d %%D in ("%TMPDIR%\extracted\*") do set "EXROOT=%%D"
if not defined EXROOT (
    echo  [ERROR] Zip contained no folder. Nothing was changed.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM ── 8. Apply over this installation ───────────────────────────────────────
echo  Applying update   : over this folder
robocopy "%EXROOT%" "%BASE%" /E /R:1 /W:1 /NFL /NDL /NJH /NP /NJS
if errorlevel 8 (
    echo  [ERROR] Applying the update failed. Restore by copying the
    echo         contents of %BK%\ back over this folder.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM ── 9. Verify + cleanup ───────────────────────────────────────────────────
set "NEWVER="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "(Select-String -Path server.py -Pattern 'HIVEMIND_VERSION\s*=\s*.([0-9.]+)').Matches[0].Groups[1].Value"`) do set "NEWVER=%%V"
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
if /I not "%NEWVER%"=="%TAGNUM%" (
    echo  [ERROR] Verification failed: server.py reports "%NEWVER%", expected "%TAGNUM%".
    echo         The update did not apply cleanly. Restore by copying the
    echo         contents of %BK%\ back over this folder.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

echo.
echo  +=============================================================+
echo  ^|    UPDATE DONE                                              ^|
echo  +=============================================================+
echo  Updated  : %CURRENT%  -^>  %NEWVER%
echo  Kept     : settings.json, models.json, .venv, llama\, logs\,
echo             sessions\ (untouched)
echo  Rollback : copy the CONTENTS of %BK%\ back over this
echo             folder, then start_hivemind.bat
echo.
echo  Start HiveMind with start_hivemind.bat. New settings keys
echo  pick up their defaults automatically.
echo.
echo  What changed / further questions:
echo    Release notes : https://github.com/%REPO%/releases/tag/v%NEWVER%
echo    Changelog     : https://github.com/%REPO%/blob/main/CHANGELOG.md
echo.
echo  Press any key to continue... & pause >nul & exit /b 0
