@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title Skill Security Review - First Setup
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Compatible Python 3.11-3.14 is resolved by the shared project bootstrap.

call "%SCRIPT_DIR%tools\resolve_python.cmd"
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  echo.
  pause
  exit /b %EXIT_CODE%
)

"%SKILL_REVIEW_RESOLVED_PYTHON%" "%SCRIPT_DIR%tools\init_project.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
