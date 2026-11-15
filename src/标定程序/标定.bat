@echo off
cd /d "%~dp0"
title Calibration Wizard

set PY=
if exist "%~dp0venv\Scripts\python.exe" set PY="%~dp0venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set PY=python
)
if not defined PY (
    echo Python not found. Install Python 3.8+ and check "Add to PATH".
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

echo ============================================
echo   Calibration Wizard
echo ============================================
echo   Ctrl+Left click  = frame the current area
echo   F8               = skip
echo   Ctrl+Right click = previous item
echo   Save writes config.json next to this file
echo.
%PY% "%~dp0dvc_calibrate.py"
echo.
echo --------------------------------------------
echo   Finished. config.json updated.
pause >nul
