#!/usr/bin/env bash
set -euo pipefail

# Legacy compatibility wrapper.
# Prefer: ./bin/bluei install-cron --repo <repo>

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${1:-}"

if [ -z "$REPO" ]; then
  echo "Usage: $0 <repo-name>" >&2
  exit 1
fi

exec "$ROOT/scripts/install-cron.sh" "$REPO"
