#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CEBIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

EMBEDDING_MODEL="${CEBIN_ROOT}/models/CEBin-Embedding-Cisco.bin"
COMPARISON_MODEL="${CEBIN_ROOT}/models/CEBin-Comparison-Cisco.bin"
SERVER_APP="${CEBIN_ROOT}/client_server/server/app.py"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9088}"
DEVICE="${DEVICE:-cuda:0}"

if [[ ! -f "${SERVER_APP}" ]]; then
  echo "[ERROR] server app not found: ${SERVER_APP}" >&2
  exit 1
fi

if [[ ! -f "${EMBEDDING_MODEL}" ]]; then
  echo "[ERROR] embedding model not found: ${EMBEDDING_MODEL}" >&2
  exit 1
fi

if [[ ! -f "${COMPARISON_MODEL}" ]]; then
  echo "[ERROR] comparison model not found: ${COMPARISON_MODEL}" >&2
  exit 1
fi

echo "[INFO] CEBIN_ROOT=${CEBIN_ROOT}"
echo "[INFO] EMBEDDING_MODEL=${EMBEDDING_MODEL}"
echo "[INFO] COMPARISON_MODEL=${COMPARISON_MODEL}"
echo "[INFO] DEVICE=${DEVICE}"
echo "[INFO] HOST=${HOST}"
echo "[INFO] PORT=${PORT}"

exec python "${SERVER_APP}" \
  --cebin-root "${CEBIN_ROOT}" \
  --embedding-model "${EMBEDDING_MODEL}" \
  --comparison-model "${COMPARISON_MODEL}" \
  --device "${DEVICE}" \
  --host "${HOST}" \
  --port "${PORT}"