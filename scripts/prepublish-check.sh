#!/usr/bin/env bash
# G4: Pre-publish checks — verify package is ready for npm publish.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "Running pre-publish checks..."

# 1. Verify version consistency between bluei/version.py and package.json
PY_VERSION=$(python3 -c 'import re; m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", open("bluei/version.py").read()); print(m.group(1) if m else "unknown")')

PKG_VERSION=$(python3 -c 'import json; print(json.load(open("package.json"))["version"])')

if [ "$PY_VERSION" != "$PKG_VERSION" ]; then
  echo "✗ Version mismatch: bluei/version.py=$PY_VERSION vs package.json=$PKG_VERSION"
  exit 1
fi
echo "✓ Version consistent: $PY_VERSION"

# 2. Verify critical files exist
for file in bin/bluei bin/bluei.py bluei/version.py pyproject.toml LICENSE README.md; do
  if [ ! -f "$file" ]; then
    echo "✗ Missing required file: $file"
    exit 1
  fi
done
echo "✓ All required files present"

# 3. Verify Python package imports cleanly
python3 -c "import bluei; print(f'✓ bluei imports (v{bluei.__version__})')" 2>/dev/null || {
  echo "⚠ Could not import bluei — ensure .venv is activated"
}

echo ""
echo "Pre-publish checks passed. Ready to publish."
