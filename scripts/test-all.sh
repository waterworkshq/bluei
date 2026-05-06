#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed/in PATH" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  uv venv .venv
fi

source .venv/bin/activate
uv pip install --quiet pytest pyyaml

export PYTHONPATH="$ROOT"
python -m pytest tests/ -q
