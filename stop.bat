@echo off
REM Stop whatever is listening on the two ports the engine uses.

setlocal enabledelayedexpansion
echo.
for %%P in (8765 20128) do (
    set "PID="
    for /f "tokens=5" %%A in ('netstat -ano ^| findstr ":%%P" ^| findstr "LISTENING"') do set "PID=%%A"
    if defined PID (
        taskkill /PID !PID! /F >NUL 2>&1
        echo   stopped port %%P  ^(pid !PID!^)
    ) else (
        echo   nothing on port %%P
    )
)
echo.
endlocal
