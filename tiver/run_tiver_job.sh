#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 /absolute/path/to/source JobName [timeout_seconds]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PATH="$1"
JOB_NAME="$2"
TIMEOUT_SECONDS="${3:-0}"

if [[ "$SOURCE_PATH" != /* ]]; then
  echo "source path must be absolute: $SOURCE_PATH" >&2
  exit 2
fi
if [[ ! -d "$SOURCE_PATH" ]]; then
  echo "source path is not a directory: $SOURCE_PATH" >&2
  exit 2
fi

python3 "$SCRIPT_DIR/runner.py" \
  --target-path "$SOURCE_PATH" \
  --job-name "$JOB_NAME" \
  --timeout-seconds "$TIMEOUT_SECONDS" \
  --jobs-dir "${TIVER_JOBS_DIR:-$SCRIPT_DIR/jobs}" \
  --docker-image "${TIVER_DOCKER_IMAGE:-geniuschoi/tiver:latest}"
