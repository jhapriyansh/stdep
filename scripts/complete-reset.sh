#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
DATA_DIR="$PROJECT_ROOT/.local-data"

if [[ "${1:-}" != "--yes" ]]; then
  cat <<'WARNING'
This permanently deletes all local StaticDeploy data:
  - administrator and member accounts
  - sessions
  - sites and domains
  - ZIP/Git deployments, previews, and rollback history

Your source code and .venv are preserved.

Run again with:
  ./scripts/complete-reset.sh --yes
WARNING
  exit 1
fi

if [[ ! -x "$VENV/bin/staticdeploy" ]]; then
  echo "StaticDeploy is not initialized. Run ./scripts/first-time-init.sh first." >&2
  exit 1
fi

case "$DATA_DIR" in
  "$PROJECT_ROOT/.local-data") ;;
  *) echo "Refusing to reset an unexpected data directory." >&2; exit 1 ;;
esac

if [[ -d "$DATA_DIR" ]]; then
  rm -rf "$DATA_DIR"
fi

"$VENV/bin/staticdeploy" --data-dir "$DATA_DIR" init
echo "Local data reset. Run ./scripts/start.sh and create a new first admin account."
