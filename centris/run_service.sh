#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not in PATH" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon is not running or current user cannot access it" >&2
  exit 1
fi

mkdir -p data work output

if [ ! -f "data/Centris_dataset.tar" ] && [ ! -d "data/runtime/preprocessor/componentDB" ]; then
  echo "ERROR: missing data/Centris_dataset.tar" >&2
  echo "Place Centris_dataset.tar under: $SCRIPT_DIR/data/Centris_dataset.tar" >&2
  exit 1
fi

IMAGE="${CENTRIS_IMAGE:-seunghoonwoo/centris_code:latest}"
if [ "${CENTRIS_SKIP_PULL:-0}" != "1" ]; then
  docker pull "$IMAGE"
fi

exec python3 app.py
