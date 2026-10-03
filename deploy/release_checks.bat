@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - RELEASE CHECKS                                ^|
echo  ^|    Run before cutting a release tag                         ^|
echo  +=============================================================+
echo.
echo  1) Full offline regression suite (all tracked suites)
echo  2) mmproj spec network check: every vision spec's projector
echo     regex must match at least one real file in its pinned
echo     HuggingFace repo (catches stale regexes after repo moves)
echo.

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    for /f "usebackq delims=" %%P in (`where python 2^>nul`) do (
        if not defined PY set "PY=%%P"
    )
)
if not defined PY (
    echo  [ERROR] No Python found (.venv\Scripts\python.exe or PATH^).
    echo  Press any key to continue... & pause >nul & exit /b 1
)

echo  === [1/2] Regression suite ===
"%PY%" tests\run_regressions.py
if errorlevel 1 (
    echo.
    echo  [ERROR] Regression suite failed - do NOT cut a release.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

echo.
echo  === [2/2] mmproj spec network check ===
REM Needs internet access; a stale regex here is the Hermes-class bug:
REM the pinned repo renamed its projector and fresh installs silently
REM got no vision support (found 2026-10-03, see tests/test_mmproj_specs.py).
"%PY%" tests\test_mmproj_specs.py --network
if errorlevel 1 (
    echo.
    echo  [ERROR] mmproj network check failed - do NOT cut a release.
    echo  Press any key to continue... & pause >nul & exit /b 1
)

echo.
echo  +=============================================================+
echo  ^|    ALL RELEASE CHECKS PASSED                                ^|
echo  +=============================================================+
echo.
echo  Press any key to continue... & pause >nul & exit /b 0
