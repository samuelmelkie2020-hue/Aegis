#!/usr/bin/env bash
# Build the Aegis desktop app for the current platform.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements-dev.txt >/dev/null

echo "Building..."
python -m PyInstaller --noconfirm --clean packaging/aegis.spec

echo
case "$(uname -s)" in
  Darwin) echo "Done: dist/Aegis.app" ;;
  *)      echo "Done: dist/Aegis" ;;
esac
