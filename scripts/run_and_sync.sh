#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT="$ROOT/qa-agent"
PYTHON_BIN="${PYTHON_BIN:-python3}"

REPO="${1:-}"
PHASE="${2:-}"
shift 2 || true

if [ -z "$REPO" ] || [ -z "$PHASE" ]; then
  echo "Usage: $0 <repo-name> <phase> [qa-agent run args...]" >&2
  exit 1
fi

RUN_STATUS=0
"$AGENT" run --repo "$REPO" --phase "$PHASE" "$@" || RUN_STATUS=$?

# Keep Obsidian as a deterministic mirror/record of qa-agent host-side state.
# Sync even on failures so monitor pages stay current.
"$PYTHON_BIN" "$ROOT/scripts/obsidian_sync.py" --repo "$REPO" --phase "$PHASE" || true
"$PYTHON_BIN" "$ROOT/scripts/obsidian_sync.py" --repo "$REPO" --phase qa-monitor || true
"$PYTHON_BIN" "$ROOT/scripts/daily_summary.py" --repo "$REPO" --format markdown || true

exit "$RUN_STATUS"
