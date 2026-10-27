@echo off
setlocal enabledelayedexpansion
REM Promote dev -> main after publishing a release.
REM main is the released state and only ever fast-forwards: if the merge is
REM not a fast-forward, something landed on main that dev does not have, and
REM this script stops instead of creating a merge commit.
REM
REM main may be checked out in a SECOND worktree (this repo keeps the stable
REM copy in ..\repo). Git refuses to switch to a branch that another worktree
REM holds, so in that case the fast-forward runs inside that worktree.
cd /d "%~dp0.."

echo  ==========================================================
echo   Promote dev to main (fast-forward only)
echo  ==========================================================
echo.

git merge-base --is-ancestor main dev
if errorlevel 1 goto diverged

set "MAIN_WT="
for /f "usebackq delims=" %%P in (`git worktree list --porcelain ^| findstr /b "worktree " ^| findstr /v /c:"worktree %CD%"`) do (
    set "CAND=%%P"
    set "CAND=!CAND:worktree =!"
    for /f "usebackq delims=" %%B in (`git -C "!CAND!" rev-parse --abbrev-ref HEAD 2^>nul`) do (
        if "%%B"=="main" set "MAIN_WT=!CAND!"
    )
)

if defined MAIN_WT goto promote_elsewhere

echo   main is checked out here: switching and fast-forwarding.
git switch main
if errorlevel 1 goto fail
git merge --ff-only dev
if errorlevel 1 goto fail
git push origin main
if errorlevel 1 goto fail
git switch dev
if errorlevel 1 goto fail
goto done

:promote_elsewhere
echo   main lives in its own worktree: %MAIN_WT%
git -C "%MAIN_WT%" merge --ff-only dev
if errorlevel 1 goto fail
git push origin main
if errorlevel 1 goto fail
goto done

:done
echo.
echo   [OK] main matches dev and is pushed.
echo   Tag the release on dev first if you have not already.
exit /b 0

:diverged
echo   [ERROR] main has commits dev does not have. Promotion stopped.
echo   Reconcile them by hand; this script never merges or forces.
exit /b 1

:fail
echo.
echo   [ERROR] Promotion stopped. Nothing was forced.
git -C "%CD%" switch dev >nul 2>&1
exit /b 1
