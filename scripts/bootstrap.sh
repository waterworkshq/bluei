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

if ! need_bin uv; then
  echo "uv is required for local environment bootstrap" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  uv venv .venv
fi

source .venv/bin/activate
uv pip install --quiet pytest pyyaml

mkdir -p repos plugins templates logs docs reports locks core

if [ ! -f core/sandbox_local_runner.py ]; then
  echo "Warning: core/sandbox_local_runner.py not found. Copy or symlink the runner before live use." >&2
fi

cat <<EOF
QA Agent bootstrap complete.

Workspace: $ROOT
Venv: $ROOT/.venv

Next steps:
  1. source .venv/bin/activate
  2. ./qa-agent doctor --format whatsapp
  3. ./qa-agent preflight --repo /path/to/repo
  4. ./qa-agent onboard --repo /path/to/repo --mode observe --profile conservative
EOF
