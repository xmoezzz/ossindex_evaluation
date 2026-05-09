#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCHIVE="${ARCHIVE:-$SCRIPT_DIR/data/OSS_version_dataset.tar.gz}"
DEST_DIR="${DEST_DIR:-$SCRIPT_DIR/data_process}"

if [[ ! -f "$ARCHIVE" ]]; then
  echo "error: archive not found: $ARCHIVE" >&2
  echo "Set ARCHIVE=/path/to/OSS_version_dataset.tar.gz if it is not under ./data/" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"

echo "archive: $ARCHIVE"
echo "destination: $DEST_DIR"
echo "checking archive..."

tar -tzf "$ARCHIVE" >/dev/null

echo "extracting..."
tar -xzf "$ARCHIVE" -C "$DEST_DIR"

echo "checking extracted dataset layout..."

if [[ -d "$DEST_DIR/dataset" ]]; then
  DATASET_DIR="$DEST_DIR/dataset"
elif [[ -d "$DEST_DIR/OSS_version_dataset/dataset" ]]; then
  DATASET_DIR="$DEST_DIR/OSS_version_dataset/dataset"
else
  echo "error: dataset directory not found after extraction under: $DEST_DIR" >&2
  echo "top-level entries:" >&2
  find "$DEST_DIR" -maxdepth 2 -mindepth 1 -print | sort | sed -n '1,80p' >&2
  exit 1
fi

echo "dataset directory: $DATASET_DIR"
echo "sample files:"
find "$DATASET_DIR" -maxdepth 6 -type f | sed -n '1,20p'

echo "done"
