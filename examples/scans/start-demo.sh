#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv venv --seed --allow-existing .venv
  elif ! python3 -m venv .venv; then
    echo 'Install Python venv support (Ubuntu/Debian: sudo apt install python3-venv), then rerun this script.' >&2
    exit 1
  fi
fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python run_demo.py "$@"
