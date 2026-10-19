@echo off
setlocal
set "EASEL_ROOT=%~dp0"
set "EASEL_PORT=7860"
for %%I in ("%~dp0.") do set "EASEL_PROJECT_ROOT=%%~fI"

powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\windows_launcher.ps1" -Action Start -ProjectRoot "%EASEL_PROJECT_ROOT%"
set "EASEL_EXIT=%ERRORLEVEL%"

if not "%EASEL_EXIT%"=="0" echo [Easel] Start failed. No unrelated process was stopped.
if not defined EASEL_NO_PAUSE pause
exit /b %EASEL_EXIT%
