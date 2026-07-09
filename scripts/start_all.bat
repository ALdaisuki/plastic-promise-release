@echo off
echo === Plastic Promise — Full System Startup ===

REM Start MCP Server
echo [1/2] Starting MCP Server on port 9020...
start /B python -m plastic_promise.mcp.server --streamable-http 9020

REM Wait for health endpoint
echo [*] Waiting for MCP server health check...
:wait_mcp
timeout /t 2 >nul
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9020/health', timeout=5)" 2>nul
if errorlevel 1 (
    echo     ... still waiting
    goto wait_mcp
)
echo     MCP Server ready.

REM Start Maintenance Daemon
echo [2/2] Starting Maintenance Daemon...
start /B python daemons/maintenance_daemon.py
timeout /t 3 >nul

echo === Plastic Promise fully started ===
echo MCP Server: http://127.0.0.1:9020
echo Daemon: running in background
echo Health: python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9020/health').read())"
