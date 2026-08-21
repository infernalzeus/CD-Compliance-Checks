@echo off
rem ============================================================
rem  CHiP-D Compliance Dashboard - double-click launcher (Windows)
rem
rem  This file must stay INSIDE the project folder, beside serve.py.
rem  First run sets everything up; later runs just start the app.
rem  Everything printed here is also written to launcher-log.txt.
rem ============================================================
setlocal
cd /d "%~dp0"
title CHiP-D Compliance Dashboard

echo(
echo   CHiP-D Compliance Dashboard
echo   ---------------------------
echo(

if not exist "serve.py" goto :wrongfolder
if not exist "requirements.txt" goto :wrongfolder

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"

rem --- 1. Python environment -----------------------------------------------
where py >nul 2>&1
if %errorlevel%==0 (set "BOOT=py -3") else (set "BOOT=python")

if not exist "%PY%" (
  echo [1/3] Setting up Python ^(first run only, please wait^)...
  %BOOT% -m venv "%VENV%"
)
if not exist "%PY%" goto :nopython

rem --- 2. Libraries ---------------------------------------------------------
"%PY%" -c "import fastapi, uvicorn, yaml, pandas" >nul 2>&1
if errorlevel 1 (
  echo [2/3] Installing required libraries ^(first run only^)...
  "%PY%" -m pip install --quiet --upgrade pip
  "%PY%" -m pip install --quiet -r requirements.txt
  if errorlevel 1 goto :pipfail
) else (
  echo [2/3] Libraries already installed.
)

rem --- 3. Analysis tools ----------------------------------------------------
if not exist "%VENV%\.tools-ok" (
  echo [3/3] Downloading the analysis tools from GitHub...
  "%PY%" setup_tools.py
  if not errorlevel 1 echo ok> "%VENV%\.tools-ok"
) else (
  echo [3/3] Analysis tools already installed.
)

echo(
echo   Starting... your browser will open at http://127.0.0.1:8000
echo   Close this window ^(or press Ctrl+C^) to stop the dashboard.
echo(
"%PY%" serve.py --open
if errorlevel 1 goto :runfail
goto :end

:wrongfolder
echo(
echo   ERROR: this launcher is not in the CD-Compliance-Checks folder.
echo(
echo   It is currently in:
echo       %CD%
echo   but serve.py / requirements.txt are not here.
echo(
echo   Move this file into the folder containing serve.py and run it there.
echo(
pause
exit /b 1

:nopython
echo(
echo   ERROR: Python was not found.
echo(
echo   Install Python 3.10 or newer from:
echo       https://www.python.org/downloads/windows/
echo   On the first installer screen, TICK "Add python.exe to PATH".
echo   Then close this window and double-click this file again.
echo(
pause
exit /b 1

:pipfail
echo(
echo   ERROR: Could not install the required libraries.
echo   Check your internet connection and try again.
echo(
pause
exit /b 1

:runfail
echo(
echo   The dashboard stopped with an error ^(see the messages above^).
echo(
pause
exit /b 1

:end
endlocal
