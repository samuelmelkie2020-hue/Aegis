#!/usr/bin/env bash
# Build the Claw desktop app for the current platform.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements-dev.txt >/dev/null

echo "Building..."
python -m PyInstaller --noconfirm --clean packaging/claw.spec

echo
case "$(uname -s)" in
  Darwin) echo "Done: dist/Claw.app" ;;
  *)      echo "Done: dist/Claw" ;;
esac
