#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8808}"

if [ ! -d "$VENV_DIR" ]; then
  echo "ERROR: virtualenv not found: $VENV_DIR" >&2
  echo "Run ./bootstrap_vulture.sh first." >&2
  exit 1
fi

source "$VENV_DIR/bin/activate"
exec uvicorn app:app --host "$HOST" --port "$PORT"
