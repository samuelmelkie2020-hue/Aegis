#!/usr/bin/env bash
# Aegis — one-click starter for macOS and Linux
set -euo pipefail

cd "$(dirname "$0")"

PORT="${AEGIS_PORT:-8000}"
URL="http://localhost:${PORT}/"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/ first."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Re-install when requirements.txt changes. A one-shot stamp file never noticed
# an edited requirements.txt, so upgrades silently kept the old packages.
DEPS_HASH="$(python -c "import hashlib,sys;print(hashlib.sha256(open('requirements.txt','rb').read()).hexdigest())")"
STAMP_FILE=".venv/.deps_hash"
if [ ! -f "$STAMP_FILE" ] || [ "$(cat "$STAMP_FILE" 2>/dev/null || true)" != "$DEPS_HASH" ]; then
  echo "Installing Aegis components, one moment..."
  python -m pip install --upgrade pip >/dev/null 2>&1 || true
  python -m pip install -r requirements.txt || {
    echo
    echo "Installation failed. Check your internet connection and try again."
    exit 1
  }
  printf '%s\n' "$DEPS_HASH" > "$STAMP_FILE"
fi

echo
echo "  =========================================="
echo "   Aegis is starting as a desktop app."
echo "   Close the window to quit."
echo "   (Browser instead: bash start.sh --web)"
echo "  =========================================="
echo

if ! python -c "import webview" >/dev/null 2>&1; then
  echo "  The desktop window needs pywebview, which is not available here."
  echo "  Falling back to the browser version..."
  echo
  exec python run.py --web "$@"
fi

exec python run.py "$@"
