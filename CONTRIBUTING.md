# Contributing to bluei

Thanks for your interest in contributing.

## License

This project is licensed under **AGPL-3.0-or-later**.

If you modify and run bluei as a network service for others, you must also make the
corresponding source code available under the same license.

The public bluei core is intended to remain open source. See [`OPEN_SOURCE_POLICY.md`](./OPEN_SOURCE_POLICY.md) for the plain-English policy.

## Contributor License Agreement (CLA)

Before contributing, please read [`CLA.md`](./CLA.md).

**By opening a pull request, you agree to the CLA.**

In short: you keep ownership of your contribution, and you grant the project owner the rights needed to use it in bluei, including in the public open-source project and in official hosted, commercial, enterprise, support, managed-service, or dual-licensed versions of bluei.

Include this line in your PR description:

> I have read and agree to the CLA in `CLA.md`.

## Development Setup

### Prerequisites

- Python 3.11 or later
- `git`
- `uv` (install: `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `gh` CLI (optional, needed for PR/issue operations)

### Bootstrap

```bash
git clone https://github.com/waterworkshq/bluei
cd bluei
./scripts/bootstrap.sh
```

This creates `.venv`, installs dependencies, and prepares workspace directories.

### Running tests

```bash
# Run all tests
./scripts/test-all.sh

# Or directly
PYTHONPATH=. python -m pytest tests/ -q

# Run a single test file
PYTHONPATH=. python -m pytest tests/test_config.py -q

# Run a single test by name
PYTHONPATH=. python -m pytest tests/test_runner.py::test_dry_run -q
```

There are 2500+ tests. All use `tmp_path` fixtures — no external services needed.

## Architecture

bluei has four layers:

| Layer | Directory | Purpose |
|-------|-----------|---------|
| **CLI** | `bin/` | `bluei` shebang entry → `bluei.py` dispatcher. Handles venv bootstrap, command routing. |
| **App layer** | `bluei/app/` | Runner, config, health, registry, onboarding, reports, emergent rules. |
| **Execution engine** | `bluei/engine/` | Discovery, fixing, PR management, batch operations, pattern learning. Largest subsystem. |
| **Review and campaigns** | `bluei/review/`, `bluei/campaigns/` | GitHub-native review lifecycle and campaign orchestration. |
| **Plugins** | `plugins/` | Per-language detection: Python, TypeScript, Go, Rust, Shell, Dockerfile, Markdown. |

For a detailed walkthrough, see [`docs/developer/ARCHITECTURE.md`](docs/developer/ARCHITECTURE.md).

## Key Concepts

- **Finding** — A detected issue (lint error, type gap, missing test, etc.)
- **Cycle** — A run phase: `issue-cycle` (discover), `pr-cycle` (fix + PR), `merge-cycle` (auto-merge), `orchestrated` (all three)
- **Care level** — Autonomy boundary: `watch-only` → `note-only` → `offer-fixes` → `full-care`
- **Pattern** — A learned fix. Generated from successful PRs, replayed deterministically on future matches.
- **Campaign** — A multi-phase batch of related fixes, planned and executed as a group.
- **Emergent rule** — A new detection rule proposed from repeated finding patterns.

## Coding Style

- Python 3.11+ with type hints where practical
- Follow conventions in neighboring files
- Keep changes scoped — one concern per PR
- Use `tmp_path` fixtures for tests that need temp directories

## Contribution Guidelines

- Keep changes scoped and well explained.
- Prefer small PRs over large unrelated bundles.
- Include tests when practical.
- Document behavior changes in README or docs/ when relevant.
- Do not remove or alter copyright/license notices.
- New language plugins should include `plugin.py`, `plugin.yaml`, and tests.
- Upstream contributions are strongly encouraged, especially for bug fixes, detector improvements, language packs, and core product improvements.

## First Contribution

1. Fork the repo on GitHub
2. Clone your fork and bootstrap (see Development Setup above)
3. Create a feature branch: `git checkout -b my-feature`
4. Make your changes and run tests: `PYTHONPATH=. python -m pytest tests/ -q`
5. Push and open a PR against `main`
6. In your PR description, include: "I have read and agree to the CLA in CLA.md."

## Ownership and Relicensing

The CLA exists so the project can remain:

- open-source for self-hosters,
- welcoming to community contributions,
- commercially sustainable for official hosted, support, managed-service, and enterprise offerings,
- and flexible for future company stewardship.

The official bluei name, logo, domain, Waterworks HQ branding, and official hosted/commercial offerings are controlled separately from the code license. See [`TRADEMARK.md`](./TRADEMARK.md).

If that model does not work for you, please do not contribute code.
