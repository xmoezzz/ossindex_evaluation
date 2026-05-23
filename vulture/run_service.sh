#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8808}"
PYTHON="${PYTHON:-python3.11}"

cd "$SCRIPT_DIR"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "ERROR: python interpreter not found: $PYTHON" >&2
  exit 1
fi

"$PYTHON" - <<'PY'
import uvicorn
import fastapi
PY

exec "$PYTHON" -m uvicorn app:app --host "$HOST" --port "$PORT"