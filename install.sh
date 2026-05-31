#!/usr/bin/env bash
set -euo pipefail

# bluei one-liner installer
# curl -fsSL https://bluei.dev/install.sh | bash

BLUEI_HOME="${BLUEI_HOME:-$HOME/.bluei}"
REPO_URL="${BLUEI_REPO_URL:-https://github.com/waterworkshq/bluei}"

# ── helpers ────────────────────────────────────────────────

info()  { printf "\033[36m>\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m!\033[0m %s\n" "$*" >&2; }
err()   { printf "\033[31m✗\033[0m %s\n" "$*" >&2; exit 1; }

need_bin() { command -v "$1" >/dev/null 2>&1; }

# ── pre-flight ─────────────────────────────────────────────

info "bluei installer"

# Platform check
case "$(uname -s)" in
    Linux|Darwin) ;;
    *) err "Unsupported platform: $(uname -s). bluei requires Linux or macOS." ;;
esac

# Python 3.11+
if ! need_bin python3; then
    err "python3 not found. Install Python 3.11+ before continuing."
fi

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$((PY_MAJOR * 100 + PY_MINOR))" -lt 311 ]; then
    err "Python 3.11+ required (found ${PY_MAJOR}.${PY_MINOR})"
fi
info "Python ${PY_MAJOR}.${PY_MINOR}"

# git
if ! need_bin git; then
    err "git not found. Install git before continuing."
fi

# uv — install if missing
if ! need_bin uv; then
    info "Installing uv..."
    curl -fsSL https://astral.sh/uv/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
    if ! need_bin uv; then
        err "uv installation failed. Install manually: https://docs.astral.sh/uv/"
    fi
fi
info "uv $(uv --version | cut -d' ' -f2)"

# gh (optional, warn only)
if ! need_bin gh; then
    warn "GitHub CLI (gh) not found — install it for PR/issue automation: https://cli.github.com/"
fi

# ── clone or update ────────────────────────────────────────

if [ -d "$BLUEI_HOME" ]; then
    info "Updating existing installation at $BLUEI_HOME"
    cd "$BLUEI_HOME"
    git fetch --quiet origin
    git reset --quiet --hard origin/main
else
    info "Cloning bluei to $BLUEI_HOME"
    git clone --quiet "$REPO_URL" "$BLUEI_HOME"
    cd "$BLUEI_HOME"
fi

# ── bootstrap ──────────────────────────────────────────────

info "Bootstrapping environment"

if [ ! -d .venv ]; then
    uv venv .venv --quiet
fi

source .venv/bin/activate

uv pip install --quiet pytest
uv pip install -e . --quiet

# Optional AST dependencies
uv pip install --quiet tree-sitter 2>/dev/null || true
uv pip install --quiet tree-sitter-typescript tree-sitter-javascript 2>/dev/null || true
uv pip install --quiet tree-sitter-go tree-sitter-rust 2>/dev/null || true

# Create runtime directories
mkdir -p repos plugins templates logs docs reports locks

# ── link to PATH ───────────────────────────────────────────

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

BLUEI_BIN="$BLUEI_HOME/bin/bluei"
if [ -f "$BLUEI_BIN" ]; then
    ln -sf "$BLUEI_BIN" "$BIN_DIR/bluei"
fi

# Add to PATH if not already there
if ! echo "$PATH" | grep -q "$BIN_DIR"; then
    case "$(basename "$SHELL")" in
        zsh)  PROFILE="$HOME/.zshrc" ;;
        bash) PROFILE="$HOME/.bashrc" ;;
        *)    PROFILE="$HOME/.profile" ;;
    esac
    if ! grep -q "$BIN_DIR" "$PROFILE" 2>/dev/null; then
        echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$PROFILE"
    fi
    export PATH="$BIN_DIR:$PATH"
    info "Added $BIN_DIR to PATH (restart your shell or run: source $PROFILE)"
fi

# ── verify ─────────────────────────────────────────────────

if need_bin bluei || [ -x "$BIN_DIR/bluei" ]; then
    VERSION=$("$BIN_DIR/bluei" --version 2>/dev/null || "$BLUEI_BIN" --version 2>/dev/null || echo "unknown")
    echo ""
    info "bluei installed successfully — $VERSION"
    echo ""
    echo "  Next steps:"
    echo "    bluei init              # onboard your first project"
    echo "    bluei doctor            # run diagnostics"
    echo ""
else
    warn "bluei may not be on PATH yet. Restart your shell or run:"
    echo "  export PATH=\"$BIN_DIR:\$PATH\""
    echo "  bluei --version"
fi
