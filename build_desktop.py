"""Assembles the Page Counter desktop folder from the current portal code, so
the desktop version and the website can't drift apart."""
import os
import shutil

SRC = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("DESKTOP_BUILD_DIR") or os.path.join(SRC, "build")
APP = os.path.join(OUT, "Page Counter")

# The working parts, shared with the website verbatim.
MODULES = ["desktop_app.py", "analyzer.py", "paper_sizes.py", "report.py",
           "structure.py", "branding.py", "version.py"]
TEMPLATES = ["desktop.html", "_head.html", "_lockup.html"]
BRAND = ["flightpath.css", "mark.svg", "mark-navy.svg", "mark-mono.svg",
         "dart.svg", "dart-on-navy.svg", "favicon.png", "favicon.ico",
         "apple-touch-icon.png", "mark-transparent.png"]

shutil.rmtree(APP, ignore_errors=True)
os.makedirs(OUT, exist_ok=True)
os.makedirs(os.path.join(APP, "templates"), exist_ok=True)
os.makedirs(os.path.join(APP, "static", "brand"), exist_ok=True)

for name in MODULES:
    shutil.copy2(os.path.join(SRC, name), os.path.join(APP, name))
for name in TEMPLATES:
    shutil.copy2(os.path.join(SRC, "templates", name), os.path.join(APP, "templates", name))
for name in BRAND:
    shutil.copy2(os.path.join(SRC, "static", "brand", name),
                 os.path.join(APP, "static", "brand", name))

# Only two third-party packages are needed now: PowerPoint and Excel are read
# with the standard library, so python-pptx and openpyxl have been dropped -
# which makes the first run quicker and gives less to go wrong.
with open(os.path.join(APP, "requirements.txt"), "w") as fh:
    fh.write("Flask>=3.0\npypdf>=4.0\nPillow>=10.0\n")

MAC = r'''#!/bin/bash
# Page Counter for Mac - double-click to start.
# First run sets up a private Python environment in this folder (a minute or two, needs internet).
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display dialog "Page Counter needs Python 3.\n\nInstall it from python.org (Downloads > macOS), then double-click Page Counter again." buttons {"OK"} default button "OK"'
  exit 1
fi

if [ ! -x ".venv/bin/python" ] || [ ! -f ".venv/.ready" ]; then
  echo ""
  echo "  Setting up Page Counter for the first time (a minute or two)..."
  echo ""
  rm -rf .venv
  if ! python3 -m venv .venv || ! .venv/bin/python -m pip install --quiet --upgrade pip \
     || ! .venv/bin/python -m pip install --quiet -r requirements.txt; then
    echo ""
    echo "  Setup didn't finish - check your internet connection and try again."
    echo "  (If it keeps failing, send Claude a screenshot of this window.)"
    read -r -p "  Press Enter to close. " _
    exit 1
  fi
  touch .venv/.ready
fi

.venv/bin/python desktop_app.py
'''

WIN = r'''@echo off
REM Page Counter for Windows - double-click to start.
REM First run sets up a private Python environment in this folder (a minute or two, needs internet).
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Page Counter needs Python 3.
  echo   Install it from https://www.python.org/downloads/ - tick "Add Python to PATH" -
  echo   then double-click Page Counter again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\.ready" (
  echo.
  echo   Setting up Page Counter for the first time (a minute or two)...
  echo.
  if exist ".venv" rmdir /s /q ".venv"
  python -m venv .venv || goto setupfailed
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip || goto setupfailed
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto setupfailed
  echo ready> ".venv\.ready"
)

".venv\Scripts\python.exe" desktop_app.py
exit /b 0

:setupfailed
echo.
echo   Setup didn't finish - check your internet connection and try again.
echo   (If it keeps failing, send Claude a screenshot of this window.)
echo.
pause
exit /b 1
'''

readme = os.path.join(SRC, "desktop", "READ ME FIRST.txt")
shutil.copy2(readme, os.path.join(APP, "READ ME FIRST.txt"))

with open(os.path.join(APP, "Page Counter.command"), "w", newline="\n") as fh:
    fh.write(MAC)
os.chmod(os.path.join(APP, "Page Counter.command"), 0o755)
with open(os.path.join(APP, "Page Counter.bat"), "w", newline="\r\n") as fh:
    fh.write(WIN)

print("built", APP)
for root, dirs, fs in os.walk(APP):
    for f in sorted(fs):
        p = os.path.join(root, f)
        print(f"  {os.path.relpath(p, APP):42s} {os.path.getsize(p):>8,} bytes")
