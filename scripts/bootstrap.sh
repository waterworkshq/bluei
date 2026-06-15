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

# G1: Install with all optional deps via pyproject.toml
uv pip install -e ".[dev,ast]"

# Verify pyyaml is available (critical dependency)
python -c "import yaml" 2>/dev/null || uv pip install pyyaml

mkdir -p repos plugins templates logs docs reports locks

# G2: Optional plugin tool installation
echo ""
echo "── Optional Plugin Tools ──────────────────────────────"
echo "bluei plugins use external linters for discovery. Install them now?"
echo ""

install_tool() {
  local tool="$1"
  local label="$2"
  local check_cmd="$3"

  if eval "$check_cmd" >/dev/null 2>&1; then
    echo "  ✓ $label already installed"
    return 0
  fi

  read -rp "  Install $label? [y/N] " response
  case "$response" in
    y|Y|yes|YES)
      echo "  Installing $tool..."
      if need_bin brew; then
        brew install "$tool" 2>/dev/null && echo "  ✓ $label installed via brew" || echo "  ✗ brew install failed — install $tool manually"
      elif need_bin apt-get; then
        sudo apt-get install -y "$tool" 2>/dev/null && echo "  ✓ $label installed via apt" || echo "  ✗ apt install failed — install $tool manually"
      else
        echo "  ✗ No package manager detected — install $tool manually"
      fi
      ;;
    *)
      echo "  Skipped $label (bluei will use text-scanning fallback)"
      ;;
  esac
}

install_tool "ruff" "ruff (Python linter — used by python plugin)" "ruff --version"
install_tool "shellcheck" "shellcheck (Shell linter)" "shellcheck --version"
install_tool "staticcheck" "staticcheck (Go analyzer)" "staticcheck -version"
install_tool "hadolint" "hadolint (Dockerfile linter)" "hadolint --version"
install_tool "markdownlint-cli" "markdownlint (Markdown linter)" "markdownlint --version"

echo "───────────────────────────────────────────────────────"

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
