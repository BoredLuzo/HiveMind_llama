@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM  install_lsp_packs.bat - optional language-server packs
REM
REM  HiveMind drives any LSP server that is on PATH. This offers the packs that
REM  were verified end-to-end on Windows (start, initialize, diagnostics).
REM
REM  Verified package sets (2026-11-02):
REM    Python      pyright
REM    TS / JS     typescript@5 + typescript-language-server
REM                (typescript must be pinned: v7 is the native port and ships
REM                 no tsserver.js, the server then refuses to start)
REM    Web         vscode-langservers-extracted + yaml-language-server
REM                (yaml is NOT part of the extracted package)
REM    Shell       bash-language-server
REM    Rust        rustup component add rust-analyzer
REM    C / C++     winget install LLVM.clangd
REM
REM  Not offered here (no one-click install):
REM    Go          needs the Go toolchain, gopls lands in %USERPROFILE%\go\bin
REM    Lua         no npm package - needs the LuaLS binary from GitHub releases
REM
REM  Usage:  deploy\install_lsp_packs.bat            (interactive)
REM          deploy\install_lsp_packs.bat 1 2 3      (packs by number)
REM          deploy\install_lsp_packs.bat --list
REM ===========================================================================
cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if /i "%~1"=="--list" goto list_packs
if not "%~1"=="" goto install_args

:list_packs
echo.
echo   [1] Python        pyright
echo   [2] TS / JS       typescript@5 + typescript-language-server
echo   [3] Web           JSON / YAML / HTML / CSS
echo   [4] Shell         bash-language-server
echo   [5] Rust          rustup component add rust-analyzer
echo   [6] C / C++       LLVM.clangd via winget ^(~250 MB^)
echo.
echo   Not offered: Go ^(needs the Go toolchain + PATH^), Lua ^(native binary^).
echo.
goto :eof

:install_args
if not "%~2"=="" goto install_multi
set "PICK=%~1"
goto install_run

:install_multi
set "PICK="
:collect_args
if "%~1"=="" goto install_run
set "PICK=!PICK! %~1"
shift
goto collect_args

:install_run
set "NPM_OK="
where npm >nul 2>&1
if not errorlevel 1 set "NPM_OK=1"

set "DO_ANY="
for %%P in (%PICK%) do (
    if "%%P"=="1" set "DO_ANY=1"
    if "%%P"=="2" set "DO_ANY=1"
    if "%%P"=="3" set "DO_ANY=1"
    if "%%P"=="4" set "DO_ANY=1"
    if "%%P"=="5" set "DO_ANY=1"
    if "%%P"=="6" set "DO_ANY=1"
)
if not defined DO_ANY goto no_pick

echo.
echo  Installing language-server packs: %PICK%
echo.

for %%P in (%PICK%) do call :pack_%%P
goto verify_packs

:pack_1
echo  --- [1] Python -----------------------------------------------
if not defined NPM_OK echo   [SKIP] npm not found - install Node.js first. & goto :eof
call npm install -g pyright
if errorlevel 1 echo   [WARN] pyright install failed.
goto :eof

:pack_2
echo  --- [2] TypeScript / JavaScript ------------------------------
if not defined NPM_OK echo   [SKIP] npm not found - install Node.js first. & goto :eof
REM typescript@5 is deliberate: v7 is the native compiler port without
REM tsserver.js, and typescript-language-server cannot start against it.
call npm install -g "typescript@5" typescript-language-server
if errorlevel 1 echo   [WARN] Typescript pack install failed.
goto :eof

:pack_3
echo  --- [3] Web ^(JSON / YAML / HTML / CSS^) ------------------------
if not defined NPM_OK echo   [SKIP] npm not found - install Node.js first. & goto :eof
call npm install -g vscode-langservers-extracted yaml-language-server
if errorlevel 1 echo   [WARN] Web pack install failed.
goto :eof

:pack_4
echo  --- [4] Shell ^(bash^) ----------------------------------------
if not defined NPM_OK echo   [SKIP] npm not found - install Node.js first. & goto :eof
call npm install -g bash-language-server
if errorlevel 1 echo   [WARN] bash-language-server install failed.
goto :eof

:pack_5
echo  --- [5] Rust ^(rust-analyzer^) --------------------------------
where rustup >nul 2>&1
if errorlevel 1 echo   [SKIP] rustup not found - install Rust first. & goto :eof
call rustup component add rust-analyzer
if errorlevel 1 echo   [WARN] rust-analyzer install failed.
goto :eof

:pack_6
echo  --- [6] C / C++ ^(clangd^) ------------------------------------
where winget >nul 2>&1
if errorlevel 1 echo   [SKIP] winget not found. & goto :eof
call winget install --id LLVM.clangd -e --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 echo   [WARN] clangd install failed - try: winget install LLVM.clangd
goto :eof

:verify_packs
echo.
echo  ------------------------------------------------------------
echo   Checking which language servers actually answer
echo  ------------------------------------------------------------
echo.
REM A server counts as usable only when it completes the LSP handshake and
REM returns diagnostics - an existing executable is not enough.
"%PY%" deploy\lsp_selftest.py
echo.
echo   Executable check without a server start:
echo     "%PY%" deploy\lsp_selftest.py ^<lang^>
echo   Go users: install the Go toolchain, then
echo     go install golang.org/x/tools/gopls@latest
echo   and add %%USERPROFILE%%\go\bin to PATH.
echo   Restart HiveMind afterwards - a running engine keeps its old PATH.
echo.
goto :eof

:no_pick
echo   Nothing selected. Packs: 
call :list_packs
goto :eof
