#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
DATA_DIR="$PROJECT_ROOT/.local-data"

cd "$PROJECT_ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it, then run this script again." >&2
  exit 1
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Creating project virtual environment..."
  python3 -m venv "$VENV"
fi

echo "Installing StaticDeploy into the project virtual environment..."
"$VENV/bin/python" -m pip install --upgrade pip setuptools
"$VENV/bin/python" -m pip install -e "$PROJECT_ROOT"

echo "Initializing local hosting data..."
"$VENV/bin/staticdeploy" --data-dir "$DATA_DIR" init

cat <<'MESSAGE'

StaticDeploy is ready.

Start it with:
  ./scripts/start.sh

Then open:
  http://localhost:8080/_platform/

Create the first administrator account there.
MESSAGE
