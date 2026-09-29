@echo off
setlocal
cd /d "%~dp0"
if /i not "%OS%"=="Windows_NT" goto failed
if not defined CONDA_PREFIX goto no_conda
set "KANPO_PYTHON=%CONDA_PREFIX%\python.exe"
if not exist "%KANPO_PYTHON%" goto no_conda
"%KANPO_PYTHON%" check_environment.py
if errorlevel 1 goto failed
"%KANPO_PYTHON%" app.py
if errorlevel 1 goto failed
exit /b 0

:no_conda
echo Activate the company-approved Miniconda environment first.
echo This script does not install packages or use another Python installation.

:failed
echo Startup failed. Check the JSON diagnostics and messages above.
echo Do not use pip or add unapproved package channels.
pause
exit /b 1
