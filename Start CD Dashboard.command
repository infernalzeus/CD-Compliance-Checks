#!/bin/bash
# ============================================================
#  CHiP-D Compliance Dashboard - double-click launcher (macOS)
#
#  IMPORTANT - this file must stay INSIDE the project folder
#  (next to serve.py). It will not work from Downloads.
#
#  If double-clicking says "permission denied", the executable
#  flag was lost when the file was copied/downloaded. Fix with:
#      chmod +x "Start CD Dashboard.command"
#  or just run it with:
#      bash "Start CD Dashboard.command"
#
#  If macOS says the file is from an unidentified developer,
#  right-click it -> Open -> Open (once only).
#
#  Everything printed here is also written to  launcher-log.txt
#  in this folder - send that file if you need help.
# ============================================================
cd "$(dirname "$0")" || exit 1

LOG="launcher-log.txt"
: > "$LOG"
# Mirror all output to the log so a failure can be sent on.
exec > >(tee -a "$LOG") 2>&1

echo "CHiP-D Compliance Dashboard"
echo "date: $(date)"
echo "folder: $(pwd)"
echo "---------------------------"
echo

pause_exit() {
  echo
  echo "  Full details were saved to: $(pwd)/$LOG"
  echo "  Press any key to close this window."
  read -r -n 1 -s
  exit 1
}

fail() {
  echo
  echo "  ERROR: $1"
  pause_exit
}

# --- 0. Are we actually in the project folder? ------------------------------
if [ ! -f "serve.py" ] || [ ! -f "requirements.txt" ]; then
  echo "  ERROR: this launcher is not in the CD-Compliance-Checks folder."
  echo
  echo "  It is currently in:"
  echo "      $(pwd)"
  echo "  but serve.py / requirements.txt are not here."
  echo
  echo "  Move this file into the CD-Compliance-Checks folder (the one"
  echo "  containing serve.py) and double-click it there."
  pause_exit
fi

# --- 1. Python environment --------------------------------------------------
VENV=".venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  BOOT=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then BOOT="$c"; break; fi
  done
  [ -n "$BOOT" ] || fail "Python was not found.
  Install Python 3.10+ from https://www.python.org/downloads/macos/
  (or run: brew install python), then double-click this file again."
  echo "[1/3] Setting up Python (first run only, please wait)..."
  echo "      using: $BOOT ($("$BOOT" --version 2>&1))"
  "$BOOT" -m venv "$VENV" || fail "Could not create the Python environment.
  On macOS this usually means the Command Line Tools are missing - run:
      xcode-select --install"
fi
[ -x "$PY" ] || fail "The Python environment is incomplete. Delete the .venv folder and try again."
echo "      python: $("$PY" --version 2>&1)"

# --- 2. Libraries -----------------------------------------------------------
if ! "$PY" -c "import fastapi, uvicorn, yaml, pandas" >/dev/null 2>&1; then
  echo "[2/3] Installing required libraries (first run only)..."
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt \
    || fail "Could not install the required libraries. Check your internet connection."
else
  echo "[2/3] Libraries already installed."
fi

# --- 3. Analysis tools ------------------------------------------------------
if [ ! -f "$VENV/.tools-ok" ]; then
  echo "[3/3] Downloading the analysis tools from GitHub..."
  if ! command -v git >/dev/null 2>&1; then
    echo "      WARNING: git is not installed, so the analysis tools cannot be"
    echo "      downloaded. Install Xcode Command Line Tools (xcode-select --install)"
    echo "      then delete .venv/.tools-ok and run this again."
  elif "$PY" setup_tools.py; then
    echo ok > "$VENV/.tools-ok"
  else
    echo "      WARNING: some tools could not be downloaded - the dashboard will"
    echo "      still start, and you can install them from its start page."
  fi
else
  echo "[3/3] Analysis tools already installed."
fi

echo
echo "  Starting... your browser will open at http://127.0.0.1:8000"
echo "  Close this window (or press Ctrl+C) to stop the dashboard."
echo
"$PY" serve.py --open || fail "The dashboard stopped with an error (see above)."
