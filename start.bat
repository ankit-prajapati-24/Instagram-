@echo off
REM Start OmniRoute and the Rahasya panel in their own windows.
REM
REM Each gets its own console so it keeps running after you close this one,
REM and so you can read its log when something misbehaves. Close a window to
REM stop that service, or run stop.bat.

setlocal
cd /d "%~dp0"

set "OMNIROUTE=%APPDATA%\npm\node_modules\omniroute\bin\omniroute.mjs"

echo.
echo   Rahasya Engine
echo   ==============
echo.

REM --- OmniRoute -----------------------------------------------------------
curl -s -m 5 -o NUL http://127.0.0.1:20128/v1/models
if %errorlevel%==0 (
    echo   [ok]    OmniRoute already running on 20128
) else (
    if exist "%OMNIROUTE%" (
        echo   [..]    starting OmniRoute ^(takes ~45s the first time^)
        start "OmniRoute" cmd /k node "%OMNIROUTE%"
    ) else (
        echo   [warn]  OmniRoute not found. Install it with:
        echo             npm install -g omniroute
        echo           The panel still works with the built-in sample script.
    )
)

REM --- the panel -----------------------------------------------------------
curl -s -m 5 -o NUL http://127.0.0.1:8765/
if %errorlevel%==0 (
    echo   [ok]    panel already running on 8765
) else (
    echo   [..]    starting the panel
    start "Rahasya panel" cmd /k python -m engine.app
)

echo.
echo   panel      http://127.0.0.1:8765
echo   gateway    http://127.0.0.1:20128
echo.
echo   The header in the panel tells you whether the gateway can actually
echo   complete a request. "no provider" means it is running but has no
echo   provider key yet - see docs\omniroute-setup.md
echo.

REM ping, not timeout: a "timeout" earlier on PATH (Git Bash ships one) can
REM shadow the Windows one, and timeout also fails when stdin is redirected.
ping -n 7 127.0.0.1 >NUL 2>&1
start "" http://127.0.0.1:8765
endlocal
