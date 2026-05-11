@echo off
setlocal ENABLEDELAYEDEXPANSION
chcp 65001 >nul

set "PROJECT_ROOT=%~dp0"
cd /d "%PROJECT_ROOT%"

if not exist ".venv\Scripts\python.exe" (
  echo [Setup] Creating virtual environment...
  py -3 -m venv .venv || goto :venv_fail
)

set "PYTHON_BIN=.venv\Scripts\python.exe"

echo [Setup] Upgrading pip...
"%PYTHON_BIN%" -m pip install -U pip || goto :pip_fail

if exist "zhuanzhuan_pricing\requirements.txt" (
  echo [Setup] Installing dependencies from zhuanzhuan_pricing\requirements.txt...
  "%PYTHON_BIN%" -m pip install -r "zhuanzhuan_pricing\requirements.txt" || goto :deps_fail
) else (
  if exist "requirements.txt" (
    echo [Setup] Installing dependencies from requirements.txt...
    "%PYTHON_BIN%" -m pip install -r "requirements.txt" || goto :deps_fail
  ) else (
    echo [Warn] requirements file not found. Continue without dependency install.
  )
)

if not exist "runtime" mkdir "runtime"
if not exist "data\browser_profiles" mkdir "data\browser_profiles"

echo.
echo [Agent] Launching interactive runner...
echo.
"%PYTHON_BIN%" -m zhuanzhuan_pricing.automation.agent_runner
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [Agent] Process exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:venv_fail
echo [Error] Failed to create virtual environment.
pause
exit /b 1

:pip_fail
echo [Error] Failed to upgrade pip.
pause
exit /b 1

:deps_fail
echo [Error] Failed to install dependencies.
pause
exit /b 1
