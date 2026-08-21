#!/bin/bash
# ============================================================
#  CHiP-D Compliance Dashboard - double-click launcher (macOS)
#  First run sets everything up; later runs just start the app.
#
#  macOS blocks downloaded .command files the first time: if
#  double-clicking is refused, right-click this file -> Open -> Open.
# ============================================================
cd "$(dirname "$0")" || exit 1

echo
echo "  CHiP-D Compliance Dashboard"
echo "  ---------------------------"
echo

VENV=".venv"
PY="$VENV/bin/python"

fail() {
  echo
  echo "  ERROR: $1"
  echo
  echo "  Press any key to close this window."
  read -r -n 1 -s
  exit 1
}

# --- 1. Python environment --------------------------------------------------
if [ ! -x "$PY" ]; then
  BOOT=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then BOOT="$c"; break; fi
  done
  [ -n "$BOOT" ] || fail "Python was not found.
  Install Python 3.10+ from https://www.python.org/downloads/macos/
  (or run: brew install python), then double-click this file again."
  echo "[1/3] Setting up Python (first run only, please wait)..."
  "$BOOT" -m venv "$VENV" || fail "Could not create the Python environment."
fi
[ -x "$PY" ] || fail "The Python environment is incomplete. Delete the .venv folder and try again."

# --- 2. Libraries -----------------------------------------------------------
if ! "$PY" -c "import fastapi, uvicorn, yaml, pandas" >/dev/null 2>&1; then
  echo "[2/3] Installing required libraries (first run only)..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt \
    || fail "Could not install the required libraries. Check your internet connection."
fi

# --- 3. Analysis tools ------------------------------------------------------
if [ ! -f "$VENV/.tools-ok" ]; then
  echo "[3/3] Downloading the analysis tools from GitHub..."
  if "$PY" setup_tools.py; then echo ok > "$VENV/.tools-ok"; fi
fi

echo
echo "  Starting... your browser will open at http://127.0.0.1:8000"
echo "  Close this window (or press Ctrl+C) to stop the dashboard."
echo
"$PY" serve.py --open || fail "The dashboard stopped with an error (see the messages above)."
