@echo off
REM ---------------------------------------------------------
REM  start_llama.bat - alias for start_hivemind.bat (compatibility)
REM  llama.cpp is found automatically: <root>\llama\<build>\llama-server.exe
REM  Override via env var HIVEMIND_LLAMA_BIN.
REM ---------------------------------------------------------
call "%~dp0start_hivemind.bat"
