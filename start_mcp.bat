@echo off
:: Starts the HiveMind MCP HTTP server on port 8090 (no conflict with llama slots from 8101).
:: Used by the IntelliJ AI Assistant.
::
:: Security hardening (2026-08-24, Tier 0.1/1.5):
::   - Binds ONLY to 127.0.0.1 (change via MCP_HTTP_BIND) + Host-Header guard.
::   - Optional: set MCP_HTTP_TOKEN -> Bearer auth is then required.
::   - Tiers: Read always on; Write via MCP_ALLOW_WRITE=1; Exec only stdio
::     (MCP_ALLOW_EXEC=1); query (Chat) opt-in via MCP_ALLOW_QUERY=1
::     (2026-08-31: query also works over HTTP when opted in).
cd /d "%~dp0"

echo.
echo  +=============================================================+
echo  ^|    HIVEMIND - MCP HTTP SERVER                               ^|
echo  ^|    AI tool server on 127.0.0.1:8090                         ^|
echo  +=============================================================+
echo.
echo  What this does:
echo    - Starts the HiveMind MCP server on http://127.0.0.1:8090.
echo    - Bound to localhost only, secured by the Host-Header guard.
echo    - Access tiers are controlled via the MCP_ALLOW_* environment
echo      variables described in the header comments above.
echo.

choice /c YN /n /m "Start the MCP server now? [Y/N] "
if errorlevel 2 (
    echo.
    echo  Aborted - server not started.
    echo.
    exit /b 0
)
echo.
echo  [..] Starting MCP HTTP server on port 8090 (127.0.0.1)...
python infra/mcp_server.py --http --port 8090
if errorlevel 1 (
    echo.
    echo  [ERROR] MCP server exited with an error. Read the message above.
    echo.
    echo  Press any key to continue...
    pause >nul
)

