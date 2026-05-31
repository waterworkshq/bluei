<!-- CATEGORY: user -->

# bluei — Installation Guide

## Quick Install

```bash
curl -fsSL https://bluei.dev/install.sh | bash
```

This handles everything: checks prerequisites, installs `uv`, clones the repo,
bootstraps the Python environment, and puts `bluei` on your PATH.

```bash
bluei --version   # verify
bluei init        # start onboarding
```

If `~/.local/bin` is not in your PATH, add this to your shell config:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Manual Install (from source)

```bash
git clone https://github.com/waterworkshq/bluei ~/.bluei
cd ~/.bluei
./scripts/bootstrap.sh
ln -sf "$(pwd)/bin/bluei" ~/.local/bin/bluei
```

## Requirements

Required:

- `python3` (3.11 or later)
- `git`
- `gh` (GitHub CLI, for PR and issue operations)

The installer auto-installs `uv` if missing.

Optional:

- `ruff`, `eslint`, `staticcheck`, `shellcheck`, and other linters (used by language plugins)
- `claude` or `opencode` (for LLM-assisted fixes)

## Bootstrap

The bootstrap script creates a Python virtual environment and installs dependencies:

```bash
./scripts/bootstrap.sh
```

This will:

- Verify core tools (python3, git, gh, uv)
- Create `.venv`
- Install test and runtime dependencies
- Create standard workspace directories

## Quick Start

### 1. Onboard a repo

```bash
bluei init --path /path/to/your-repo
```

Use the interactive wizard (`bluei init` with no args) or headless mode:

```bash
bluei onboard --repo /path/to/target-repo --mode watch-only --profile conservative
```

### 2. Check status

```bash
bluei status
bluei status --repo my-repo
```

### 3. Scan for issues

```bash
bluei duster my-repo    # dry-run — preview without creating anything
bluei scan my-repo      # run discovery and create issues
```

### 4. Fix issues

```bash
bluei clean my-repo     # fix findings and open pull requests
```

### 5. Monitor health

```bash
bluei health my-repo
bluei report my-repo --format html
```

### 6. Schedule automation

```bash
bluei install-cron my-repo
```

## Care Levels

| Level | Description |
|-------|-------------|
| `watch-only` | Dry-run only. No issues, PRs, or merges created. |
| `note-only` | Create issues, no PRs or merges. |
| `offer-fixes` | Create issues and PRs, no auto-merge. |
| `full-care` | Full autonomy — issues, PRs, and merges. |

## Upgrade

```bash
bluei update
```

Or re-run the installer — it's idempotent.

## Related Documentation

- [Architecture](../developer/ARCHITECTURE.md) — System architecture and components
- [Configuration Reference](../reference/CONFIG_REFERENCE.md) — All configuration options
- [Operator Guide](../operator/OPERATOR_GUIDE.md) — Day-to-day operation
- [Language Packs](../developer/LANGUAGE_PACKS.md) — Detection plugins
