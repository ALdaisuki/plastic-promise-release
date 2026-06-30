@echo off
setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set PID_DIR=%SCRIPT_DIR%.interop\.pid
set NO_NEKO=0
set STATUS_ONLY=0

:parse_args
if "%~1"=="" goto :start
if "%~1"=="--no-neko" set NO_NEKO=1
if "%~1"=="--status" set STATUS_ONLY=1
shift
goto :parse_args

:start
if not exist "%PID_DIR%" mkdir "%PID_DIR%"

if "%STATUS_ONLY%"=="1" (
    echo === agent-interop services ===
    if exist "%PID_DIR%\*.pid" (
        for %%f in ("%PID_DIR%\*.pid") do (
            set /p PID=<"%%f"
            set NAME=%%~nf
            echo   ● !NAME! (pid=!PID!)
        )
    ) else (
        echo   No services running.
    )
    goto :end
)

cd /d "%SCRIPT_DIR%.."

echo [launcher] Starting event-bus...
start "event-bus" /B npx tsx bridge/event-bus.ts > NUL 2>&1
echo   OK event-bus

if "%NO_NEKO%"=="0" (
    echo [launcher] Starting neko-adapter...
    start "neko-adapter" /B python bridge/neko_adapter.py > NUL 2>&1
    echo   OK neko-adapter
)

echo.
echo === All services started ===
echo Press Ctrl+C in each window to stop, or close them manually.
echo.

:end
endlocal
