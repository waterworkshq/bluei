#!/usr/bin/env bash
set -euo pipefail

# Legacy compatibility wrapper.
# Prefer: ./qa-agent install-cron --repo <repo>

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${1:-ky}"

exec "$ROOT/scripts/install-cron.sh" "$REPO"
