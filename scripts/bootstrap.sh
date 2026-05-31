#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

need_bin() {
  command -v "$1" >/dev/null 2>&1
}

missing=()
for bin in python3 git gh; do
  if ! need_bin "$bin"; then
    missing+=("$bin")
  fi
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required tools: ${missing[*]}" >&2
  exit 1
fi

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$((PY_MAJOR * 100 + PY_MINOR))" -lt 311 ]; then
  echo "Python 3.11+ required (found ${PY_MAJOR}.${PY_MINOR})" >&2
  exit 1
fi

if ! need_bin uv; then
  echo "uv is required for local environment bootstrap" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  uv venv .venv
fi

source .venv/bin/activate
uv pip install --quiet pytest
uv pip install -e .

# Verify pyyaml is available (critical dependency)
python -c "import yaml" 2>/dev/null || uv pip install pyyaml

uv pip install --quiet tree-sitter 2>/dev/null || true
uv pip install --quiet tree-sitter-typescript tree-sitter-javascript 2>/dev/null || true
uv pip install --quiet tree-sitter-go tree-sitter-rust 2>/dev/null || true

mkdir -p repos plugins templates logs docs reports locks

cat <<EOF
bluei bootstrap complete.

Workspace: $ROOT
Venv: $ROOT/.venv

Next steps:
  1. source .venv/bin/activate
  2. bluei --version         # verify
  3. bluei doctor             # run diagnostics
  4. bluei preflight --repo /path/to/repo
  5. bluei onboard --repo /path/to/repo --mode watch-only --profile conservative
EOF
