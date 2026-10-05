@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - PACKAGE RELEASE                               ^|
echo  ^|    Builds the release zip from the GIT TREE (git archive)   ^|
echo  +=============================================================+
echo.
echo  The zip contains exactly the tracked files - never a working
echo  directory with settings.json, presets.json, models or sessions
echo  (the git token lives in settings.json; a dirty zip leaks it).
echo.

set "VER="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -Command "(Select-String -Path server.py -Pattern 'HIVEMIND_VERSION\s*=\s*.([0-9][0-9.a-z-]*)').Matches[0].Groups[1].Value"`) do set "VER=%%V"
if not defined VER (
    echo  [ERROR] Could not read HIVEMIND_VERSION from server.py.
    echo  Press any key to continue... & pause >nul & exit /b 1
)
set "ROOTNAME=HiveMind_v%VER%_release"
set "OUT=%ROOTNAME%.zip"

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Not a git repository - releases are built from the tree.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

echo  Version  : %VER%
echo  Output   : ..\%OUT%  ^(one run folder deep: %ROOTNAME%\ - what
echo              update.bat^'s extract step expects^)
echo.
git archive --format=zip --output="..\%OUT%" --prefix="%ROOTNAME%/" HEAD
if errorlevel 1 (
    echo  [ERROR] git archive failed.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

REM SAFETY CHECK: the zip must not contain any user-state file.
REM Gateway entries (2026-10-05, WP1): gateway.toml (owner config) and the
REM state/audit/kill-switch files. The tracked gateway.toml.example does
REM NOT match the anchored names below.
powershell -NoProfile -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem; $z = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path '..\%OUT%')); $bad = $z.Entries | Where-Object { $_.FullName -match '(^|/)(settings\.json|presets\.json|models\.json|vision_model\.json|routing_weights\.json|memory\.json|gateway\.toml|gateway_state\.json|gateway_state\.json\.corrupt|gateway\.disabled|gateway\.lock|gateway_audit\.jsonl)$' -or $_.FullName -match '(^|/)sessions/' }; if ($bad) { $bad | ForEach-Object { Write-Output ('LEAK: ' + $_.FullName) }; $z.Dispose(); exit 1 }; Write-Output ('entries: ' + $z.Entries.Count); $z.Dispose()"
if errorlevel 1 (
    echo  [ERROR] Zip contains user-state files - NOT usable as a release.
    echo         Fix the tracked tree; nothing was published.
    del "..\%OUT%"
    echo  Press any key to continue... & pause >nul & exit /b 1
)
echo  [OK] zip clean: no settings/presets/models/sessions inside.
echo.
REM BUILD INFO (2026-10-04): embed the exact git build into the zip so a
REM released install can tell what it runs (start_hivemind.bat prints it;
REM live is NOT a git repo, so 'git describe' there is impossible).
set "BUILD_DESC="
for /f "usebackq delims=" %%D in (`git describe --tags --always`) do set "BUILD_DESC=%%D"
if not defined BUILD_DESC set "BUILD_DESC=%VER%"
set "BUILD_DATE="
for /f "usebackq delims=" %%D in (`git log -1 --format^=%%ci`) do set "BUILD_DATE=%%D"
powershell -NoProfile -Command "$zipPath = (Resolve-Path ('..\' + $env:OUT)).ToString(); Add-Type -AssemblyName System.IO.Compression.FileSystem; $zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Update'); $entry = $zip.CreateEntry($env:ROOTNAME + '/BUILD_INFO.txt'); $sw = New-Object System.IO.StreamWriter($entry.Open()); $sw.WriteLine('version: ' + $env:VER); $sw.WriteLine('build: ' + $env:BUILD_DESC); $sw.WriteLine('commit_date: ' + $env:BUILD_DATE); $sw.Dispose(); $zip.Dispose()"
if errorlevel 1 (
    echo  [WARN] Could not embed BUILD_INFO.txt - banner will show no build line.
) else (
    echo  [OK] BUILD_INFO.txt embedded: %BUILD_DESC%
)
echo.
echo  Done. Upload ..\%OUT% as the release asset (tag v%VER%).
echo  Press any key to continue... & pause >nul & exit /b 0
