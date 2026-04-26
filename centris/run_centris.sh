#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${SCRIPT_DIR}/data"
OUTPUT_DIR="${SCRIPT_DIR}/output"
DATA_TAR="${DATA_DIR}/Centris_dataset.tar"
IMAGE="${CENTRIS_IMAGE:-seunghoonwoo/centris_code:latest}"
PLATFORM="${CENTRIS_PLATFORM:-linux}"

usage() {
  cat >&2 <<USAGE
Usage:
  $0 /absolute/path/to/target/source [package_name]

Environment variables:
  CENTRIS_IMAGE       Docker image to use. Default: seunghoonwoo/centris_code:latest
  CENTRIS_PLATFORM    Platform argument passed to Detector.py. Default: linux

Required dataset input:
  Put the CENTRIS dataset tarball at:
    data/Centris_dataset.tar

The script extracts it automatically when componentDB/metaInfos are not already present.
It also accepts an already-extracted dataset containing:
  componentDB/
  metaInfos/

Optional dataset directories:
  aveFuncs/
  weights/
  funcDate/
  verIDX/
USAGE
}

canonical_dir() {
  cd "$1" && pwd -P
}

find_dataset_roots() {
  if [ -d "$DATA_DIR/componentDB" ] && [ -d "$DATA_DIR/metaInfos" ]; then
    canonical_dir "$DATA_DIR"
    return 0
  fi

  find "$DATA_DIR" -maxdepth 4 -type d -name componentDB -print 2>/dev/null | while IFS= read -r component_dir; do
    parent_dir="$(dirname "$component_dir")"
    if [ -d "$parent_dir/metaInfos" ]; then
      canonical_dir "$parent_dir"
    fi
  done | sort -u
}

resolve_dataset_root() {
  mkdir -p "$DATA_DIR"

  roots_file="$(mktemp)"
  trap 'rm -f "$roots_file"' EXIT

  find_dataset_roots > "$roots_file"

  if [ ! -s "$roots_file" ]; then
    if [ -f "$DATA_TAR" ]; then
      echo "Extracting CENTRIS dataset: $DATA_TAR"
      tar -xf "$DATA_TAR" -C "$DATA_DIR"
      find_dataset_roots > "$roots_file"
    fi
  fi

  root_count="$(wc -l < "$roots_file" | tr -d '[:space:]')"

  if [ "$root_count" = "0" ]; then
    echo "ERROR: CENTRIS dataset was not found." >&2
    echo "Expected either:" >&2
    echo "  $DATA_TAR" >&2
    echo "or an extracted dataset containing componentDB/ and metaInfos/." >&2
    exit 1
  fi

  if [ "$root_count" != "1" ]; then
    echo "ERROR: multiple CENTRIS dataset roots were found. Keep exactly one dataset under data/." >&2
    cat "$roots_file" >&2
    exit 1
  fi

  sed -n '1p' "$roots_file"
}

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  usage
  exit 2
fi

TARGET_DIR="$1"
PACKAGE_NAME="${2:-$(basename "$TARGET_DIR")}"

if [ -z "$PACKAGE_NAME" ]; then
  echo "ERROR: package name is empty" >&2
  exit 1
fi

if [[ "$PACKAGE_NAME" == *"/"* ]]; then
  echo "ERROR: package name must not contain '/'" >&2
  exit 1
fi

if [ ! -d "$TARGET_DIR" ]; then
  echo "ERROR: target source directory does not exist: $TARGET_DIR" >&2
  exit 1
fi

case "$TARGET_DIR" in
  /*) ;;
  *)
    echo "ERROR: target source directory must be an absolute path: $TARGET_DIR" >&2
    exit 1
    ;;
esac

DATASET_ROOT="$(resolve_dataset_root)"

for required_dir in componentDB metaInfos; do
  if [ ! -d "$DATASET_ROOT/$required_dir" ]; then
    echo "ERROR: missing CENTRIS dataset directory: $DATASET_ROOT/$required_dir" >&2
    exit 1
  fi
done

mkdir -p "$OUTPUT_DIR"

TTY_ARGS=()
if [ -t 0 ] && [ -t 1 ]; then
  TTY_ARGS=(-it)
fi

docker run --rm "${TTY_ARGS[@]}" \
  -v "$TARGET_DIR:/target:ro" \
  -v "$DATASET_ROOT:/centris_data:ro" \
  -v "$OUTPUT_DIR:/centris_output" \
  -e CENTRIS_PACKAGE_NAME="$PACKAGE_NAME" \
  -e CENTRIS_PLATFORM="$PLATFORM" \
  "$IMAGE" \
  /bin/bash -lc '
    set -euo pipefail

    cd /home/code

    if [ ! -f ./Detector.py ]; then
      echo "ERROR: Detector.py was not found under /home/code in the Docker image" >&2
      exit 1
    fi

    for required_dir in componentDB metaInfos; do
      if [ ! -d "/centris_data/${required_dir}" ]; then
        echo "ERROR: missing /centris_data/${required_dir}" >&2
        exit 1
      fi
    done

    for data_dir in componentDB metaInfos aveFuncs weights funcDate verIDX; do
      rm -rf "./${data_dir}"
      if [ -d "/centris_data/${data_dir}" ]; then
        ln -s "/centris_data/${data_dir}" "./${data_dir}"
      fi
    done

    rm -rf "./res/${CENTRIS_PACKAGE_NAME}"

    python3 ./Detector.py "/target" "${CENTRIS_PACKAGE_NAME}" 0 "${CENTRIS_PLATFORM}"

    if [ ! -d "./res/${CENTRIS_PACKAGE_NAME}" ]; then
      echo "ERROR: CENTRIS did not create result directory: ./res/${CENTRIS_PACKAGE_NAME}" >&2
      exit 1
    fi

    rm -rf "/centris_output/${CENTRIS_PACKAGE_NAME}"
    cp -a "./res/${CENTRIS_PACKAGE_NAME}" "/centris_output/${CENTRIS_PACKAGE_NAME}"
  '

echo "CENTRIS result: ${OUTPUT_DIR}/${PACKAGE_NAME}"
