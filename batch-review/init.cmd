@echo off
setlocal
chcp 65001 >nul
title Skill Security Review - First Setup
set "SCRIPT_DIR=%~dp0"

if defined SKILL_REVIEW_PYTHON (
  "%SKILL_REVIEW_PYTHON%" "%SCRIPT_DIR%tools\init_project.py"
) else (
  py -3.12 "%SCRIPT_DIR%tools\init_project.py"
)

set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
