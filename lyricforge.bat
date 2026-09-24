@echo off
title lyricforge
cd /d "%~dp0"

if not exist "%~dp0requirements.txt" goto wrongfolder
if not exist "%~dp0lyricforge\__init__.py" goto wrongfolder

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"

if exist "%VPY%" goto deps

echo.
echo   First run - setting things up. This takes a minute or two.
echo.

py -3 --version >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv "%VENV%"
    goto venvmade
)

python --version >nul 2>&1
if not errorlevel 1 (
    python -m venv "%VENV%"
    goto venvmade
)

goto nopython

:venvmade
if not exist "%VPY%" goto venvfailed

:deps
"%VPY%" -c "import numpy, PIL, imageio_ffmpeg" >nul 2>&1
if not errorlevel 1 goto run

echo   Installing what it needs (one time only)...
echo.
"%VPY%" -m pip install --disable-pip-version-check --quiet -r "%~dp0requirements.txt"
if errorlevel 1 goto depsfailed

:run
echo.
echo   Starting lyricforge - your browser will open in a moment.
echo.
echo   Keep THIS window open while you work.
echo   Close it, or press Ctrl+C, when you are finished.
echo.
"%VPY%" -m lyricforge ui
goto done

:nopython
echo.
echo   Python was not found on this computer.
echo.
echo   1. Install Python 3.10 or newer from  https://www.python.org/downloads/
echo   2. IMPORTANT: tick "Add python.exe to PATH" on the first installer screen.
echo   3. Run this file again.
echo.
pause
exit /b 1

:venvfailed
echo.
echo   Could not set up the Python environment in:
echo     %VENV%
echo.
echo   If this folder is inside OneDrive or a synced folder, try moving
echo   lyricforge somewhere simple such as C:\lyricforge and run it again.
echo.
pause
exit /b 1

:wrongfolder
echo.
echo   This launcher is not inside the lyricforge project folder.
echo.
echo   It is sitting in:
echo     %~dp0
echo.
echo   It has to live next to requirements.txt and the lyricforge folder.
echo.
echo   To fix it:
echo     1. Go to  https://github.com/Punisher920/lyricforge
echo     2. Green "Code" button, then "Download ZIP"
echo     3. Extract it somewhere simple such as  C:\lyricforge
echo     4. Double-click this file from INSIDE that folder
echo.
echo   Avoid the Desktop and other OneDrive folders - OneDrive syncing
echo   interferes with the Python environment this creates.
echo.
pause
exit /b 1

:depsfailed
echo.
echo   Installing the dependencies failed. The error from pip is above.
echo.
echo   Common causes:
echo     - No internet connection, or a network that blocks pip
echo     - This folder is inside OneDrive; try C:\lyricforge instead
echo.
pause
exit /b 1

:done
echo.
echo   lyricforge has stopped.
echo.
pause
