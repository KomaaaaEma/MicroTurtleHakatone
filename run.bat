@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set "studio_exit=%errorlevel%"
if not "%studio_exit%"=="0" pause
exit /b %studio_exit%
