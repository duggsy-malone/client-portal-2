@echo off
rem Page Counter for Windows - double-click to start.
rem First run sets up a private Python environment in this folder (a minute or two, needs internet).
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)
%PY% --version >nul 2>nul
if not %errorlevel%==0 (
  echo.
  echo   Page Counter needs Python 3. Install it from python.org, tick "Add Python to PATH",
  echo   then double-click Page Counter again.
  pause
  exit /b 1
)

if not exist ".venv\.ready" (
  echo.
  echo   Setting up Page Counter for the first time ^(a minute or two^)...
  if exist ".venv" rmdir /s /q ".venv"
  %PY% -m venv .venv || goto setupfailed
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto setupfailed
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto setupfailed
  type nul > ".venv\.ready"
)

".venv\Scripts\python.exe" desktop_app.py
exit /b 0

:setupfailed
echo.
echo   Setup didn't finish - check your internet connection and try again.
pause
exit /b 1
