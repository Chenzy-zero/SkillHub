@echo off
setlocal

set "SCRIPT_DIR=%~dp0"

if defined SKILL_REVIEW_PYTHON (
  "%SKILL_REVIEW_PYTHON%" "%SCRIPT_DIR%tools\run_skill_batch.py" %*
) else (
  py -3.12 "%SCRIPT_DIR%tools\run_skill_batch.py" %*
)

exit /b %ERRORLEVEL%
