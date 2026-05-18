#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="${PYTHON:-python3.11}"
HOST="${MOVERY_HOST:-0.0.0.0}"
PORT="${MOVERY_PORT:-8808}"
APP_MODULE="${MOVERY_APP_MODULE:-movery_service_app:app}"

export MOVERY_JOBS_DIR="${MOVERY_JOBS_DIR:-$SCRIPT_DIR/data/jobs}"
export MOVERY_DOCKER_IMAGE="${MOVERY_DOCKER_IMAGE:-seunghoonwoo/movery-public:latest}"
export MOVERY_SERVER_WORKERS="${MOVERY_SERVER_WORKERS:-8}"
export MOVERY_QUEUE_MAX_SIZE="${MOVERY_QUEUE_MAX_SIZE:-$(( MOVERY_SERVER_WORKERS * 4 > 8 ? MOVERY_SERVER_WORKERS * 4 : 8 ))}"

mkdir -p "$MOVERY_JOBS_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker not found in PATH" >&2
  exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: python executable not found: $PYTHON" >&2
  exit 1
fi

if ! "$PYTHON" -c 'import fastapi, uvicorn, multipart' >/dev/null 2>&1; then
  echo "error: required Python modules are missing for $PYTHON" >&2
  echo "install: $PYTHON -m pip install fastapi uvicorn python-multipart" >&2
  exit 1
fi

echo "MOVERY service"
echo "  python: $PYTHON"
echo "  app: $APP_MODULE"
echo "  bind: $HOST:$PORT"
echo "  jobs: $MOVERY_JOBS_DIR"
echo "  image: $MOVERY_DOCKER_IMAGE"
echo "  workers: $MOVERY_SERVER_WORKERS"
echo "  queue_max_size: $MOVERY_QUEUE_MAX_SIZE"

docker image inspect "$MOVERY_DOCKER_IMAGE" >/dev/null 2>&1 || docker pull "$MOVERY_DOCKER_IMAGE"

cd "$SCRIPT_DIR"
exec "$PYTHON" -m uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT"
