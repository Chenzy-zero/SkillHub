@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "TOOLS_DIR=%~dp0"
for %%I in ("%TOOLS_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "LOCAL_PYTHON=%PROJECT_ROOT%\.scanner-tools\_python313\python.exe"
set "INSTALLER=%PROJECT_ROOT%\packages\python-3.13.15-amd64.exe"
set "EXPECTED_SHA256=edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403"
set "RESOLVED_PYTHON="

rem An explicit override remains authoritative, but validate it before use.
if defined SKILL_REVIEW_PYTHON (
  call :check_python "%SKILL_REVIEW_PYTHON%"
  if errorlevel 1 (
    echo Error: SKILL_REVIEW_PYTHON is not a supported Python 3.11-3.14 interpreter: "%SKILL_REVIEW_PYTHON%" 1>&2
    exit /b 2
  )
  set "RESOLVED_PYTHON=%SKILL_REVIEW_PYTHON%"
  goto :resolved
)

rem Prefer the project-owned 3.13 runtime when it was installed previously.
if exist "%LOCAL_PYTHON%" (
  call :check_python "%LOCAL_PYTHON%"
  if not errorlevel 1 (
    set "RESOLVED_PYTHON=%LOCAL_PYTHON%"
    goto :resolved
  )
)

rem Fall back to an already installed compatible system Python.
for %%V in (3.14 3.13 3.12 3.11) do (
  if not defined RESOLVED_PYTHON (
    for /f "usebackq delims=" %%P in (`py -%%V -c "import sys; raise SystemExit(1) if sys.version_info[:2] not in ((3,11),(3,12),(3,13),(3,14)) else print(sys.executable)" 2^>nul`) do set "RESOLVED_PYTHON=%%P"
  )
)
if defined RESOLVED_PYTHON goto :resolved

for /f "usebackq delims=" %%P in (`python -c "import sys; raise SystemExit(1) if sys.version_info[:2] not in ((3,11),(3,12),(3,13),(3,14)) else print(sys.executable)" 2^>nul`) do set "RESOLVED_PYTHON=%%P"
if defined RESOLVED_PYTHON goto :resolved

rem No system Python exists. Bootstrap the repository-bundled Python 3.13
rem directly from CMD so review.cmd/init.cmd can start without Python preinstalled.
if not exist "%INSTALLER%" (
  echo Error: no compatible Python was found and the bundled installer is missing: "%INSTALLER%" 1>&2
  exit /b 2
)

set "SKILL_REVIEW_BOOTSTRAP_INSTALLER=%INSTALLER%"
set "ACTUAL_SHA256="
for /f "usebackq delims=" %%H in (`powershell.exe -NoProfile -NonInteractive -Command "$h=(Get-FileHash -LiteralPath $env:SKILL_REVIEW_BOOTSTRAP_INSTALLER -Algorithm SHA256).Hash; if($h){$h.ToLowerInvariant()}" 2^>nul`) do set "ACTUAL_SHA256=%%H"
if not defined ACTUAL_SHA256 (
  for /f "skip=1 tokens=* delims=" %%H in ('certutil.exe -hashfile "%INSTALLER%" SHA256 2^>nul') do (
    if not defined ACTUAL_SHA256 set "ACTUAL_SHA256=%%H"
  )
  if defined ACTUAL_SHA256 set "ACTUAL_SHA256=!ACTUAL_SHA256: =!"
)
if /I not "!ACTUAL_SHA256!"=="%EXPECTED_SHA256%" (
  echo Error: bundled Python 3.13 installer SHA-256 verification failed. 1>&2
  exit /b 2
)

if not exist "%PROJECT_ROOT%\.scanner-tools" mkdir "%PROJECT_ROOT%\.scanner-tools" >nul 2>&1
start "" /wait "%INSTALLER%" /quiet InstallAllUsers=0 TargetDir="%PROJECT_ROOT%\.scanner-tools\_python313" Include_launcher=0 InstallLauncherAllUsers=0 Include_pip=1 Include_test=0 Include_doc=0 Include_tcltk=0 Include_symbols=0 Include_debug=0 Shortcuts=0 AssociateFiles=0 PrependPath=0 AppendPath=0
if errorlevel 1 (
  echo Error: bundled Python 3.13 installer failed with exit code !ERRORLEVEL!. 1>&2
  exit /b 2
)

if not exist "%LOCAL_PYTHON%" (
  echo Error: bundled Python 3.13 installation did not create "%LOCAL_PYTHON%". 1>&2
  exit /b 2
)
call :check_python "%LOCAL_PYTHON%"
if errorlevel 1 (
  echo Error: bundled Python 3.13 installation is not executable. 1>&2
  exit /b 2
)
set "RESOLVED_PYTHON=%LOCAL_PYTHON%"

goto :resolved

:check_python
"%~1" -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3,11),(3,12),(3,13),(3,14)) else 1)" >nul 2>&1
exit /b %ERRORLEVEL%

:resolved
endlocal & set "SKILL_REVIEW_RESOLVED_PYTHON=%RESOLVED_PYTHON%"
exit /b 0
