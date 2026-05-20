#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

: "${TIVER_HOST:=0.0.0.0}"
: "${TIVER_PORT:=8808}"
: "${TIVER_JOBS_DIR:=$SCRIPT_DIR/jobs}"
: "${TIVER_DOCKER_IMAGE:=geniuschoi/tiver:latest}"

mkdir -p "$TIVER_JOBS_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker command not found; install Docker or set up the Docker CLI before starting the service" >&2
  exit 1
fi

if ! docker image inspect "$TIVER_DOCKER_IMAGE" >/dev/null 2>&1; then
  echo "TIVER Docker image not found locally: $TIVER_DOCKER_IMAGE" >&2
  echo "Pulling $TIVER_DOCKER_IMAGE ..." >&2
  docker pull "$TIVER_DOCKER_IMAGE"
fi

: "${TIVER_SERVER_WORKERS:=1}"
export TIVER_JOBS_DIR
export TIVER_DOCKER_IMAGE
export TIVER_SERVER_WORKERS

python3 -m uvicorn app:app --host "$TIVER_HOST" --port "$TIVER_PORT"
