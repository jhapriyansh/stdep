#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_ROOT/.venv"
DATA_DIR="$PROJECT_ROOT/.local-data"
PORT="${PORT:-8080}"
ROUTING_MODE="${STATICDEPLOY_ROUTING_MODE:-path}"

if [[ ! -x "$VENV/bin/staticdeploy" ]]; then
  echo "StaticDeploy is not initialized. Run ./scripts/first-time-init.sh first." >&2
  exit 1
fi

if [[ "$ROUTING_MODE" != "path" && "$ROUTING_MODE" != "subdomain" ]]; then
  echo "STATICDEPLOY_ROUTING_MODE must be path or subdomain." >&2
  exit 1
fi

echo "Starting StaticDeploy at http://localhost:$PORT"
echo "Routing mode: $ROUTING_MODE"
echo "Press Ctrl+C to stop."

exec "$VENV/bin/staticdeploy" \
  --data-dir "$DATA_DIR" \
  serve \
  --host 127.0.0.1 \
  --port "$PORT" \
  --routing-mode "$ROUTING_MODE"
