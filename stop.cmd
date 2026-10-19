@echo off
setlocal
set "EASEL_ROOT=%~dp0"
set "EASEL_PORT=7860"
for %%I in ("%~dp0.") do set "EASEL_PROJECT_ROOT=%%~fI"

powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\windows_launcher.ps1" -Action Stop -ProjectRoot "%EASEL_PROJECT_ROOT%"
set "EASEL_EXIT=%ERRORLEVEL%"

if not "%EASEL_EXIT%"=="0" echo [Easel] Stop refused because process ownership could not be proven.
if not defined EASEL_NO_PAUSE pause
exit /b %EASEL_EXIT%
