@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
set "SCRIPT_DIR=%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if defined SKILL_REVIEW_PYTHON (
  "%SKILL_REVIEW_PYTHON%" "%SCRIPT_DIR%tools\project_status.py" %*
  exit /b %ERRORLEVEL%
)

for %%V in (3.14 3.13 3.12 3.11) do (
  if not defined SKILL_REVIEW_PYTHON_VERSION (
    py -%%V -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13),(3,14)) else 1)" >nul 2>&1
    if not errorlevel 1 set "SKILL_REVIEW_PYTHON_VERSION=%%V"
  )
)

if defined SKILL_REVIEW_PYTHON_VERSION (
  py -!SKILL_REVIEW_PYTHON_VERSION! "%SCRIPT_DIR%tools\project_status.py" %*
  exit /b %ERRORLEVEL%
)

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13),(3,14)) else 1)" >nul 2>&1
if not errorlevel 1 (
  python "%SCRIPT_DIR%tools\project_status.py" %*
  exit /b %ERRORLEVEL%
)

echo Error: Python 3.11-3.14 was not found. 1>&2
exit /b 2
