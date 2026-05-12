#!/usr/bin/env bash
#
# blenny install.sh — Install the QA Agent CLI into your PATH
#
# Usage:  bash scripts/install.sh
#
# This script:
#   1. Checks for Python 3.11+
#   2. Creates ~/.local/bin if missing
#   3. Symlinks bin/blenny → ~/.local/bin/blenny
#   4. Runs bootstrap if .venv is missing
#
# Idempotent — safe to run multiple times.
#

set -euo pipefail

# Resolve the QA Agent workspace (parent of scripts/)
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CEPH_SCRIPT="$INSTALL_DIR/bin/blenny"
TARGET_DIR="${HOME}/.local/bin"
TARGET_LINK="${TARGET_DIR}/blenny"

# ── Color helpers ────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; RESET='\033[0m'
else
    BOLD=''; GREEN=''; YELLOW=''; RED=''; RESET=''
fi

echo ""
echo "${BOLD}Blenny — Installer${RESET}"
echo "━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Check for Python 3.11+ ──────────────────────────────────────
echo -n "🔍 Checking Python version... "
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver="$("$candidate" --version 2>&1 | grep -oP '[0-9]+\.[0-9]+')"
        major="${ver%.*}"
        minor="${ver#*.}"
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$candidate"
            echo "found $("$PYTHON" --version 2>&1)"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo ""
    echo -e "${RED}✗ Python 3.11+ is required but not found.${RESET}"
    echo "  Install Python 3.11 or later: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

# ── Check for blenny script ───────────────────────────────────────
if [[ ! -f "$CEPH_SCRIPT" ]]; then
    echo -e "${RED}✗ blenny script not found at: $CEPH_SCRIPT${RESET}"
    echo "  Make sure you're running this from the qa-agent directory."
    exit 1
fi

# ── Create target dir ────────────────────────────────────────────
mkdir -p "$TARGET_DIR"
echo -e "📁 Target directory: ${BOLD}$TARGET_DIR${RESET}"

# ── Symlink blenny ─────────────────────────────────────────────────
if [[ -L "$TARGET_LINK" ]]; then
    existing="$(readlink "$TARGET_LINK")"
    if [[ "$existing" == "$CEPH_SCRIPT" ]]; then
        echo -e "🔗 Symlink already exists: ${GREEN}$TARGET_LINK → $CEPH_SCRIPT${RESET}"
    else
        echo -e "🔄 Updating symlink: ${YELLOW}$existing → $CEPH_SCRIPT${RESET}"
        ln -sf "$CEPH_SCRIPT" "$TARGET_LINK"
    fi
elif [[ -f "$TARGET_LINK" ]]; then
    echo -e "⚠️  File exists at $TARGET_LINK (not a symlink). ${YELLOW}Overwriting.${RESET}"
    rm -f "$TARGET_LINK"
    ln -s "$CEPH_SCRIPT" "$TARGET_LINK"
else
    ln -s "$CEPH_SCRIPT" "$TARGET_LINK"
    echo -e "🔗 Created symlink: ${GREEN}$TARGET_LINK → $CEPH_SCRIPT${RESET}"
fi

# ── Make blenny executable ─────────────────────────────────────────
chmod +x "$CEPH_SCRIPT"

# ── Bootstrap if needed ──────────────────────────────────────────
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    echo ""
    echo "⚙️  Running bootstrap (no .venv found)..."
    if [[ -f "$INSTALL_DIR/scripts/bootstrap.sh" ]]; then
        bash "$INSTALL_DIR/scripts/bootstrap.sh"
    else
        echo -e "${YELLOW}  ⚠ bootstrap.sh not found — run it manually later.${RESET}"
    fi
else
    echo -e "✅  Virtual environment: ${GREEN}$INSTALL_DIR/.venv${RESET}"
fi

# ── Check PATH ───────────────────────────────────────────────────
echo ""
echo -e "📋 Checking PATH..."
if [[ ":$PATH:" == *":$TARGET_DIR:"* ]]; then
    echo -e "${GREEN}✅ $TARGET_DIR is in your PATH.${RESET}"
else
    echo -e "${YELLOW}⚠  $TARGET_DIR is NOT in your PATH.${RESET}"
    echo ""
    echo "  Add this to ~/.bashrc, ~/.zshrc, or equivalent:"
    echo ""
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    echo "  Then reload: source ~/.bashrc"
fi

# ── Done ─────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}✅ Blenny installed successfully!${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Quick test:"
echo "    blenny --version"
echo "    blenny help"
echo "    blenny init"
echo ""
echo "  Documentation:"
echo "    blenny help          # CLI reference"
echo "    blenny help install  # Install instructions"
echo "    cat README.md      # Full docs"
echo ""
