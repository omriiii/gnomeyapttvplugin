@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   Gnome Bot Launcher
echo ============================================
echo.

REM --- Find a Python interpreter (prefer the "py" launcher) ---
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PYLAUNCH=py"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL%==0 (
        set "PYLAUNCH=python"
    ) else (
        echo [ERROR] Python wasn't found on this machine.
        echo.
        echo Install it from https://www.python.org/downloads/
        echo IMPORTANT: on the first install screen, check the box that says
        echo "Add python.exe to PATH" -- then run this script again.
        echo.
        pause
        exit /b 1
    )
)

REM --- Create the virtual environment on first run only ---
if not exist "venv\Scripts\python.exe" (
    echo Setting up a virtual environment - this only happens once...
    %PYLAUNCH% -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo [ERROR] Couldn't create the virtual environment. See the message above.
        pause
        exit /b 1
    )
)

set "VENV_PY=venv\Scripts\python.exe"

REM --- Install/update dependencies every run (fast no-op if already current) ---
echo Checking dependencies...
"%VENV_PY%" -m pip install --upgrade pip --quiet
"%VENV_PY%" -m pip install -r requirements.txt
if not %ERRORLEVEL%==0 (
    echo [ERROR] Installing dependencies failed. See the message above.
    pause
    exit /b 1
)

echo.
echo Starting the bot...
echo Status page: http://127.0.0.1:8420/  (opening it in your browser shortly)
echo Leave this window open while you stream. Close it (or Ctrl+C) to stop.
echo.

REM Give the bot a couple seconds to come up, then open the status page
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8420/"

"%VENV_PY%" twitch_gnome_bot.py

echo.
echo Bot stopped.
pause
