@echo off
setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

echo [Update] Fetching latest code...
git fetch --all --prune || goto :git_fail
git pull --ff-only || goto :git_fail

echo.
echo [Update] Starting agent with latest code...
call "%PROJECT_ROOT%启动改价Agent.bat"
set "EXIT_CODE=%ERRORLEVEL%"
exit /b %EXIT_CODE%

:git_fail
echo [Error] git update failed. Please resolve branch conflicts or local changes first.
pause
exit /b 1
