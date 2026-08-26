@echo off
setlocal
cd /d "%~dp0"
echo Starting SkillHub Gerrit Change Discovery POC deployment...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
set CODE=%ERRORLEVEL%
if not "%CODE%"=="0" (
  echo.
  echo Deployment failed. Exit code: %CODE%
  pause
  exit /b %CODE%
)
echo.
echo Deployment completed.
pause
