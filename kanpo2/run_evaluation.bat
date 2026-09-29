@echo off
setlocal
if not defined LOCALAPPDATA goto failed
if not exist "%~dp0KanpoChecker.exe" goto missing_exe
echo Starting KanpoChecker with a separate evaluation data folder.
echo Data: "%LOCALAPPDATA%\KanpoChecker-Evaluation"
start "" /wait "%~dp0KanpoChecker.exe" --data-dir "%LOCALAPPDATA%\KanpoChecker-Evaluation"
if errorlevel 1 goto failed
exit /b 0

:missing_exe
echo This launcher is for the completed EXE distribution.
echo Build the EXE first, then run this file beside KanpoChecker.exe.
goto failed

:failed
echo Evaluation startup failed. Read the application error message.
pause
exit /b 1
