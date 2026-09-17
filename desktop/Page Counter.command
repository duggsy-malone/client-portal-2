#!/bin/bash
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
