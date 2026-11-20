@echo off
setlocal
title Aegis - Your Aegis
cd /d "%~dp0"

if not "%AEGIS_PORT%"=="" (set "PORT=%AEGIS_PORT%") else (set "PORT=8000")

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python was not found on this computer.
  echo  Install it from https://www.python.org/downloads/
  echo  IMPORTANT: tick the box "Add python.exe to PATH" during install.
  echo.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo.
    echo  Could not create the virtual environment.
    echo  Try: python -m venv .venv
    echo.
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

rem Re-install whenever requirements.txt changes, instead of trusting a stamp
rem file that never noticed an edited requirements file.
set "STAMP=.venv\.deps_hash"
for /f "delims=" %%H in ('python -c "import hashlib;print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())"') do set "DEPS_HASH=%%H"
set "STAMPED="
if exist "%STAMP%" set /p STAMPED=<"%STAMP%"

if not "%STAMPED%"=="%DEPS_HASH%" (
  echo Installing Aegis components, one moment...
  python -m pip install --upgrade pip >nul 2>nul
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  Installation failed. Check your internet connection and double-click me again.
    echo.
    pause
    exit /b 1
  )
  > "%STAMP%" echo %DEPS_HASH%
)

echo.
echo    ==========================================
echo     Aegis is starting as a desktop app.
echo     Close the window to quit.
echo     (Browser instead: start.bat --web)
echo    ==========================================
echo.

python -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo   The desktop window needs pywebview, which is not available here.
  echo   Falling back to the browser version...
  echo.
  python run.py --web %*
) else (
  python run.py %*
)

pause
