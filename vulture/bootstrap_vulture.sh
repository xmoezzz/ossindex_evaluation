#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="$SCRIPT_DIR/vendor"
REPO_DIR="$VENDOR_DIR/Vulture"
VENV_DIR="$SCRIPT_DIR/.venv"

mkdir -p "$VENDOR_DIR"

if [ ! -d "$REPO_DIR/.git" ]; then
  git clone https://github.com/ShangzhiXu/Vulture.git "$REPO_DIR"
fi

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "$SCRIPT_DIR/requirements-service.txt"
python -m pip install -r "$REPO_DIR/requirements.txt"

echo "Vulture repo: $REPO_DIR"
echo "Virtualenv: $VENV_DIR"
echo "You still need system tools such as clang-format and ctags if they are not installed."
