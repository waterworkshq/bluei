#!/usr/bin/env bash
#
# bluei install.sh — One-command installer
#
# Usage (remote):
#   curl -fsSL https://bluei.dev/install.sh | bash
#
# Usage (local, from repo root):
#   bash scripts/install.sh
#
# This script:
#   1. Checks for Python 3.11+ and git
#   2. Auto-installs uv if missing
#   3. Clones waterworkshq/bluei to ~/.bluei (or pulls if exists)
#   4. Bootstraps the Python environment
#   5. Symlinks bin/bluei → ~/.local/bin/bluei
#
# Idempotent — safe to run multiple times.
#

set -euo pipefail

BLUEI_REPO="https://github.com/waterworkshq/bluei.git"
INSTALL_DIR="${HOME}/.bluei"
BLUEI_SCRIPT="$INSTALL_DIR/bin/bluei"
TARGET_DIR="${HOME}/.local/bin"
TARGET_LINK="${TARGET_DIR}/bluei"

# ── Color helpers ────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD='\033[1m'; GREEN='\033[32m'; YELLOW='\033[33m'; RED='\033[31m'; DIM='\033[2m'; RESET='\033[0m'
else
    BOLD=''; GREEN=''; YELLOW=''; RED=''; DIM=''; RESET=''
fi

echo ""
echo -e "${BOLD}bluei — Installer${RESET}"
echo -e "${DIM}Trust the silence.${RESET}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── Step 1: Check for git ───────────────────────────────────────
echo -n "Checking git... "
if command -v git &>/dev/null; then
    echo -e "${GREEN}found${RESET}"
else
    echo ""
    echo -e "${RED}✗ git is required but not found.${RESET}"
    echo "  Install git: https://git-scm.com/downloads"
    exit 1
fi

# ── Step 2: Check for Python 3.11+ ──────────────────────────────
echo -n "Checking Python... "
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver="$("$candidate" --version 2>&1 | grep -oP '[0-9]+\.[0-9]+' | head -1)"
        major="${ver%.*}"
        minor="${ver#*.}"
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$candidate"
            echo -e "${GREEN}$("$PYTHON" --version 2>&1)${RESET}"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo ""
    echo -e "${RED}✗ Python 3.11+ is required but not found.${RESET}"
    echo "  Install Python 3.11 or later: https://www.python.org/downloads/"
    exit 1
fi

# ── Step 3: Install uv if missing ───────────────────────────────
echo -n "Checking uv... "
if command -v uv &>/dev/null; then
    echo -e "${GREEN}found${RESET}"
else
    echo -e "${YELLOW}not found — installing...${RESET}"
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if command -v uv &>/dev/null; then
            echo -e "  ${GREEN}uv installed successfully.${RESET}"
        else
            echo -e "${RED}✗ uv installed but not found on PATH.${RESET}"
            echo "  Restart your terminal and re-run this installer."
            exit 1
        fi
    else
        echo -e "${RED}✗ uv installation failed.${RESET}"
        echo "  Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
fi

# ── Step 4: Clone or update repo ────────────────────────────────
if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo -n "Updating bluei (~/.bluei)... "
    cd "$INSTALL_DIR"
    if git pull --ff-only --quiet 2>/dev/null; then
        echo -e "${GREEN}done${RESET}"
    else
        echo -e "${YELLOW}could not pull — using existing checkout${RESET}"
    fi
else
    if [[ -d "$INSTALL_DIR" ]]; then
        echo -e "${YELLOW}~/.bluei exists but is not a git repo. Removing and re-cloning.${RESET}"
        rm -rf "$INSTALL_DIR"
    fi
    echo -n "Cloning bluei to ~/.bluei... "
    if git clone --quiet "$BLUEI_REPO" "$INSTALL_DIR"; then
        echo -e "${GREEN}done${RESET}"
    else
        echo ""
        echo -e "${RED}✗ Failed to clone repository.${RESET}"
        echo "  Check that https://github.com/waterworkshq/bluei exists and is accessible."
        exit 1
    fi
fi
cd "$INSTALL_DIR"

# ── Step 5: Verify clone integrity ──────────────────────────────
if [[ ! -f "$BLUEI_SCRIPT" ]]; then
    echo -e "${RED}✗ bin/bluei not found in cloned repo. Clone may be corrupted.${RESET}"
    echo "  Try: rm -rf $INSTALL_DIR && re-run this installer."
    exit 1
fi

# ── Step 6: Bootstrap ───────────────────────────────────────────
echo -n "Bootstrapping environment... "
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    echo ""
    if bash "$INSTALL_DIR/scripts/bootstrap.sh"; then
        if [[ -d "$INSTALL_DIR/.venv" ]]; then
            echo -e "${GREEN}Bootstrapping complete.${RESET}"
        else
            echo -e "${RED}✗ Bootstrap ran but .venv was not created.${RESET}"
            echo "  Try running manually: bash $INSTALL_DIR/scripts/bootstrap.sh"
            exit 1
        fi
    else
        echo -e "${RED}✗ Bootstrap failed.${RESET}"
        echo "  Try running manually: bash $INSTALL_DIR/scripts/bootstrap.sh"
        exit 1
    fi
else
    echo -e "${GREEN}venv exists${RESET}"
fi

# ── Step 7: Symlink ─────────────────────────────────────────────
mkdir -p "$TARGET_DIR"
chmod +x "$BLUEI_SCRIPT"

if [[ -L "$TARGET_LINK" ]]; then
    existing="$(readlink "$TARGET_LINK")"
    if [[ "$existing" == "$BLUEI_SCRIPT" ]]; then
        echo -e "Symlink: ${GREEN}$TARGET_LINK → $BLUEI_SCRIPT${RESET}"
    else
        ln -sf "$BLUEI_SCRIPT" "$TARGET_LINK"
        echo -e "Symlink updated: ${YELLOW}$existing → $BLUEI_SCRIPT${RESET}"
    fi
elif [[ -f "$TARGET_LINK" ]]; then
    rm -f "$TARGET_LINK"
    ln -s "$BLUEI_SCRIPT" "$TARGET_LINK"
    echo -e "Symlink created: ${GREEN}$TARGET_LINK → $BLUEI_SCRIPT${RESET}"
else
    ln -s "$BLUEI_SCRIPT" "$TARGET_LINK"
    echo -e "Symlink created: ${GREEN}$TARGET_LINK → $BLUEI_SCRIPT${RESET}"
fi

# ── Step 8: Verify ──────────────────────────────────────────────
if "$TARGET_LINK" --version &>/dev/null; then
    VERSION="$("$TARGET_LINK" --version 2>&1 | head -1)"
    echo -e "Verification: ${GREEN}$VERSION${RESET}"
else
    echo -e "${YELLOW}⚠ bluei --version failed. The symlink exists but bluei may not work yet.${RESET}"
    echo "  Try opening a new terminal and running: bluei --version"
fi

# ── Step 9: Check PATH ──────────────────────────────────────────
echo ""
if [[ ":$PATH:" == *":$TARGET_DIR:"* ]]; then
    echo -e "${GREEN}$TARGET_DIR is in your PATH.${RESET}"
else
    echo -e "${YELLOW}$TARGET_DIR is NOT in your PATH.${RESET}"
    echo ""
    echo "  Add this to your shell config (~/.bashrc, ~/.zshrc, etc.):"
    echo ""
    echo -e "    ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
    echo ""
    echo "  Then reload: source ~/.bashrc"
fi

# ── Done ────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}bluei installed.${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Next steps:"
echo "    bluei --version"
echo "    bluei init"
echo ""
echo "  Update anytime:"
echo "    bluei update"
echo "    # or re-run this installer"
echo ""
