@echo off
setlocal
cd /d "%~dp0"
if /i not "%OS%"=="Windows_NT" goto failed
if not defined CONDA_PREFIX goto no_conda
set "KANPO_PYTHON=%CONDA_PREFIX%\python.exe"
if not exist "%KANPO_PYTHON%" goto no_conda

"%KANPO_PYTHON%" -B build_windows.py
if errorlevel 1 goto failed
pause
exit /b 0

:no_conda
echo Activate the company-approved Miniconda environment first.
echo This script does not install packages or use another Python installation.
goto failed

:failed
echo Build failed. Check the new build_runs folder and its logs if one was created.
echo Do not distribute an older build as the result of this failed run.
echo Do not use pip or add unapproved package channels.
pause
exit /b 1
