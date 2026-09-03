@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title Skill Security Review - First Setup
set "SCRIPT_DIR=%~dp0"

if defined SKILL_REVIEW_PYTHON (
  "%SKILL_REVIEW_PYTHON%" "%SCRIPT_DIR%tools\init_project.py"
) else (
  for %%V in (3.14 3.13 3.12 3.11) do (
    if not defined SKILL_REVIEW_PYTHON_VERSION (
      py -%%V -c "import sys; raise SystemExit(0 if (3, 11) ^<= sys.version_info[:2] ^< (3, 15) else 1)" >nul 2>&1
      if not errorlevel 1 set "SKILL_REVIEW_PYTHON_VERSION=%%V"
    )
  )
  if defined SKILL_REVIEW_PYTHON_VERSION (
    py -!SKILL_REVIEW_PYTHON_VERSION! "%SCRIPT_DIR%tools\init_project.py"
  ) else (
    python -c "import sys; raise SystemExit(0 if (3, 11) ^<= sys.version_info[:2] ^< (3, 15) else 1)" >nul 2>&1
    if not errorlevel 1 (
      python "%SCRIPT_DIR%tools\init_project.py"
    ) else (
      echo Error: Python 3.11-3.14 was not found. 1>&2
      cmd /c exit 2
    )
  )
)

set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%
