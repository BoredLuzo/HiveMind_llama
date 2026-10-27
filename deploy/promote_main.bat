@echo off
setlocal
REM Promote dev -> main after publishing a release.
REM main is the released state and only ever fast-forwards: if the merge is
REM not a fast-forward, something landed on main that dev does not have, and
REM this script stops instead of creating a merge commit.
cd /d "%~dp0.."

echo  ==========================================================
echo   Promote dev to main (fast-forward only)
echo  ==========================================================
echo.
git switch main
if errorlevel 1 goto fail
git merge --ff-only dev
if errorlevel 1 goto fail
git push origin main
if errorlevel 1 goto fail
git switch dev
if errorlevel 1 goto fail
echo.
echo   [OK] main matches dev and is pushed.
echo   Tag the release on dev first if you have not already.
exit /b 0

:fail
echo.
echo   [ERROR] Promotion stopped. Nothing was forced.
echo   Check "git status" and whether main has commits dev lacks.
git switch dev >nul 2>&1
exit /b 1
