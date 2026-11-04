@echo off
rem ============================================================
rem  SangforBox portable build - double-click to run
rem  Output: ..\SangforBox-Portable\ and ..\SangforBox-Portable.zip
rem  Extra args are passed through, e.g.:
rem      build-portable.cmd -OutputDirectory D:\out\SangforBox
rem ============================================================
setlocal
cd /d "%~dp0"

echo ==========================================================
echo  SangforBox - portable build (EXE + WorkBuddy skills)
echo ==========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Preparing virtual environment and dependencies...
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
    if errorlevel 1 goto failed
) else (
    echo [1/3] Virtual environment found, skipping setup.
)

echo [2/3] Building portable package...
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\package-portable.ps1" %*
if errorlevel 1 goto failed

echo.
echo [3/3] Build finished.
echo.
for %%I in ("%~dp0..") do set "OUTROOT=%%~fI"
echo   Folder : %OUTROOT%\SangforBox-Portable
echo   Archive: %OUTROOT%\SangforBox-Portable.zip
echo.
pause
exit /b 0

:failed
echo.
echo BUILD FAILED - see the messages above.
echo.
pause
exit /b 1
