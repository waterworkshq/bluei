<!-- CATEGORY: developer -->

# bluei — Testing Guide

## Philosophy

bluei's test suite is **unit-first, deterministic, and fully offline**. Every test runs in
isolation without network calls, Docker containers, `gh` auth tokens, or real GitHub
repositories. Tests that need filesystem state use pytest's `tmp_path` fixture, which
provides a clean temporary directory per test invocation.

Key principles:

- **No external services.** No GitHub API, no `gh` CLI, no network. Everything is mocked
  or constructed in-memory.
- **Deterministic.** Same test, same result, every time. No flaky timeouts or race
  conditions.
- **`tmp_path` for state.** Tests create repos, configs, and state directories inside
  `tmp_path` — never touching the real workspace.
- **Fast.** 5000+ tests run in ~60 seconds. No heavy setup or teardown.

## Running Tests

### Prerequisites

Tests require Python 3.11+ and `uv`. Bootstrap the environment first:

```bash
./scripts/bootstrap.sh
```

This creates `.venv` and installs `pytest` and `pyyaml`.

### Run All Tests

The canonical way — handles venv activation and `PYTHONPATH` automatically:

```bash
./scripts/test-all.sh
```

### Run Tests Manually

If your shell already has the venv activated:

```bash
PYTHONPATH=. python -m pytest tests/ -q
```

`PYTHONPATH=.` is **required**. Tests import from `bluei`, and the package lives at the
repo root — not inside a standard `site-packages`.

### Run a Single File

```bash
PYTHONPATH=. python -m pytest tests/test_config.py -q
```

### Run a Single Test by Name

```bash
PYTHONPATH=. python -m pytest tests/test_runner.py::test_dry_run -q
```

### Run with Verbose Output

```bash
PYTHONPATH=. python -m pytest tests/test_health.py -v
```

### Run a Test File Directly

Most test files include a `__main__` block, so you can run them standalone:

```bash
PYTHONPATH=. python tests/test_config.py
```

## Test Organization

### Directory Structure

```
tests/
  conftest.py          # Shared fixtures (make_finding, git_repo, etc.) + global hooks
  test_config.py       # ConfigManager, RepoConfig, config migration
  test_models.py       # Data models, round-trip serialization, enums
  test_health.py       # Health engine, scoring, baselines
  test_runner.py       # Run engine, CLI arg building, state persistence
  test_onboard.py      # Language detection, framework detection, full onboarding
  test_plugins.py      # Plugin loading, discovery, language packs
  test_registry.py     # Repo registry CRUD
  test_state.py        # State manager, run history
  test_review*.py      # Review lifecycle, autonomous review, publish filters
  test_e2e_*.py        # End-to-end integration tests within features
  test_*.py            # ...150+ more test files covering all subsystems
```

### Naming Conventions

| Pattern | Meaning |
|---------|---------|
| `test_<module>.py` | Tests for `bluei/app/<module>.py` |
| `test_<feature>_<aspect>.py` | Specific aspect of a feature (e.g., `test_pattern_confidence.py`) |
| `test_<feature>_e2e.py` | End-to-end integration within the feature |

### Test Count

The suite has **170 test files** containing **5100+ individual tests**. All pass without
network access.

## Writing a Test

### Step-by-Step Example

Here's a complete test for a hypothetical `bluei/app/scorer.py` module, showing how shared
fixtures from `conftest.py` and local fixtures work together:

```python
#!/usr/bin/env python3
"""Tests for bluei/app/scorer.py."""

import pytest

from bluei.app.scorer import ScoreEngine, ScoreResult


# --- Option A: Use shared fixture from conftest.py ---

def test_finding_scored_correctly(make_finding):
    f = make_finding(rule="ruff-c408", confidence=0.9)
    result = ScoreEngine.score(f)
    assert result.passed


def test_app_finding_scored(make_app_finding):
    f = make_app_finding(severity="high", category="style")
    result = ScoreEngine.score(f)
    assert result.band == "poor"


# --- Option B: Local fixture for domain-specific setup ---

@pytest.fixture
def engine():
    return ScoreEngine()


def test_empty_input_returns_zero(engine):
    result = engine.calculate([])
    assert result.score == 0.0


# --- Option C: Class-based grouping ---

class TestScoreBands:
    def test_excellent_band(self):
        result = ScoreResult(score=95.0, band="")
        assert result.band == "excellent"

    def test_poor_band(self):
        result = ScoreResult(score=25.0, band="")
        assert result.band == "poor"


# --- Option D: Chaining shared + local fixtures ---

@pytest.fixture
def workspace_with_config(git_repo):
    config = git_repo / "bluei.yaml"
    config.write_text("scoring:\n  weights:\n    style: 0.5\n")
    return git_repo


def test_loads_config_from_repo(workspace_with_config, git_commit_all):
    git_commit_all(workspace_with_config, "add config")
    engine = ScoreEngine.from_repo(workspace_with_config)
    assert engine.weights["style"] == 0.5
```

### Choosing a Test Style

| Style | When to use | Example in codebase |
|-------|-------------|---------------------|
| **Shared fixture param** | Standard data (Finding, git repo) | `make_finding` in `test_batch_execution.py` |
| **Standalone function** | Simple input/output assertions | `test_health.py` |
| **Class-based** | Grouped tests sharing setup/teardown | `test_config.py::TestConfigManager` |
| **Local fixture** | Domain-specific shared setup | `test_runner.py::runner_env` |
| **Chained fixtures** | Build on shared or local fixture | `workspace_with_config` building on `git_repo` |

## Common Fixture Patterns

### `tmp_path` — Isolated Filesystem

Every test that touches the filesystem uses `tmp_path`. Never write to `/tmp` directly.

```python
def test_save_config(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = ConfigManager(workspace=workspace)
    # ...
```

For tests using the older `setup_method`/`teardown_method` pattern, `tempfile.mkdtemp()` is
used with manual cleanup — but `tmp_path` is preferred for new tests.

### Multi-Component Workspace Fixture

Tests that need a full engine stack create a workspace fixture:

```python
@pytest.fixture
def runner_env(tmp_path):
    workspace = tmp_path / 'bluei'
    (workspace / 'repos').mkdir(parents=True)
    (workspace / 'plugins').mkdir()
    (workspace / 'logs').mkdir()
    (workspace / 'core').mkdir()

    config = ConfigManager(workspace)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    runner = RunEngine(registry, state, health, config)
    return {
        'workspace': workspace,
        'config': config,
        'registry': registry,
        'health': health,
        'state': state,
        'runner': runner,
    }
```

### Chained Fixtures

Build derived state on top of base fixtures:

```python
@pytest.fixture
def test_repo(runner_env):
    repo_config = RepoConfig(
        id='test-001',
        name='test-repo',
        path='/tmp/test-repo',
        language='python',
    )
    return runner_env['registry'].create(repo_config)
```

### Mocking with `monkeypatch`

For tests that need to fake subprocess calls or module-level functions:

```python
def test_apply_claude_fix_cleans_up_prompt_file(tmp_path, monkeypatch):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()

    captured = {}

    def fake_run(*args, **kwargs):
        captured['cwd'] = kwargs.get('cwd')
        return SimpleNamespace(returncode=0, stdout='ok')

    monkeypatch.setattr(slr.subprocess, 'run', fake_run)
    # ... call the function under test ...
    assert captured['cwd'] == str(worktree)
```

### Shared Fixtures from `conftest.py`

The test suite provides centralized factory fixtures in `tests/conftest.py`. Use these
instead of defining local `make_finding()` or `git init` helpers in individual test files.

#### `make_finding` — Engine-layer Finding factory

Creates `bluei.engine.models.Finding` instances with sensible defaults. Accepts `**overrides`
for any field.

```python
def test_something(make_finding):
    f = make_finding()                          # all defaults
    f = make_finding(rule="broad-except", path="src/app.py")  # override specific fields
```

Default values: `finding_id="f001"`, `repo="test-repo"`, `path="src/main.py"`, `line=42`,
`rule="ruff-c408"`, `snippet="dict(a=1)"`, `confidence=0.72`, `quick_win=True`,
`safe_to_autofix=True`.

Used by ~30 test files covering the engine layer (batch, pattern, cascade, orchestrator, etc.).

#### `make_app_finding` — App-layer Finding factory

Creates `bluei.app.models.Finding` instances. Same `**overrides` interface as `make_finding`,
but for the app-layer model which has `to_dict()`, `category`, and `severity` fields.

```python
def test_health_score(make_app_finding):
    f = make_app_finding(safe_to_autofix=False, severity="high")
    assert f.to_dict()["severity"] == "high"
```

Used by files that interact with `StateManager`, `HealthEngine`, or the campaign planner
(e.g., `test_health.py`, `test_state.py`, `test_e2e_campaign_lifecycle.py`).

#### `git_repo` — Initialized git repository

Returns a `Path` to a fully initialized git repo (init + config + initial commit with README).
Each test gets its own isolated repo via `tmp_path`.

```python
def test_dirty_detection(git_repo):
    (git_repo / "new_file.py").write_text("x = 1")
    result = subprocess.run(["git", "status", "--porcelain"], cwd=str(git_repo), capture_output=True)
    assert result.stdout.strip()  # dirty worktree
```

#### `git_commit_all` — Stage-and-commit helper

Factory fixture returning a function `(repo_path, message="init")` that runs `git add .` +
`git commit`. Useful for creating intermediate commits during test setup.

```python
def test_rebase(git_repo, git_commit_all):
    (git_repo / "feature.py").write_text("pass")
    git_commit_all(git_repo, "add feature")
```

#### `mock_smtp_server` — Pre-configured SMTP mock

A `MagicMock` with `__enter__` returning self and `__exit__` returning `False`. Used by
email notifier tests to avoid setting up the context manager boilerplate.

```python
def test_email_sent(mock_smtp_server):
    with patch("bluei.engine.notifiers.email.smtplib.SMTP", return_value=mock_smtp_server):
        notifier.send(payload)
    mock_smtp_server.send_message.assert_called_once()
```

### When to Add a Local Helper vs. Use a Shared Fixture

| Situation | Approach |
|-----------|----------|
| Standard `Finding` construction | Use `make_finding` / `make_app_finding` fixture |
| Standard git repo setup | Use `git_repo` / `git_commit_all` fixture |
| Domain-specific test data (e.g., `make_pattern()`, `make_batch_group()`) | Local helper in the test file |
| Fixture that builds on `tmp_path` with extra structure | Local fixture (can chain off shared ones) |
| SMTP mocking | Use `mock_smtp_server` fixture |

### Plugin Fixtures

Tests that need real plugins available copy them from the project's `plugins/` directory:

```python
@pytest.fixture
def onboard_env(tmp_path):
    workspace = tmp_path / 'bluei'
    (workspace / 'repos').mkdir(parents=True)
    (workspace / 'plugins').mkdir()

    # Copy real plugins into test workspace
    source_plugins_dir = Path(__file__).resolve().parents[1] / 'plugins'
    for plugin_dir in source_plugins_dir.iterdir():
        if plugin_dir.is_dir():
            target = workspace / 'plugins' / plugin_dir.name
            target.mkdir(parents=True, exist_ok=True)
            for child in plugin_dir.iterdir():
                if child.is_file():
                    (target / child.name).write_text(child.read_text())
    # ...
```

## Test Discovery Configuration

`pytest.ini` at the repo root controls discovery:

```ini
[pytest]
testpaths = tests
norecursedirs =
    repos
    worktrees
    .git
    .venv
    node_modules
    logs
    state
    state-archive
    reports
```

This ensures pytest only looks under `tests/` and skips runtime directories that may
contain cloned repos, logs, or generated state.

## Global Hooks & Shared Fixtures

`tests/conftest.py` provides two things:

### Pattern Cache Clearing (hook)

A `pytest_runtest_setup` hook clears the pattern replay cache between tests to prevent
cross-test contamination from learned fix patterns:

```python
def pytest_runtest_setup(item):
    try:
        from bluei.engine.pattern_replay import _clear_pattern_cache
        _clear_pattern_cache()
    except Exception:
        pass
```

### Shared Fixtures

Five pytest fixtures are defined in `conftest.py` and available to all test files without
imports (see [Common Fixture Patterns](#shared-fixtures-from-conftestpy) above):

| Fixture | Returns | Used by |
|---------|---------|---------|
| `make_finding` | Factory → `engine.models.Finding` | ~30 files |
| `make_app_finding` | Factory → `app.models.Finding` | ~3 files |
| `git_repo` | `Path` (initialized repo) | ~10 files |
| `git_commit_all` | Factory → `(path, msg)` function | ~10 files |
| `mock_smtp_server` | Pre-configured `MagicMock` | 1 file (9 tests) |

## Coverage Expectations

- **All new code should include tests.** There is no strict coverage percentage gate, but
  every bug fix, new feature, or behavior change should have a corresponding test.
- **Round-trip tests** — data models (`Finding`, `HealthScore`, `Baseline`, `RepoConfig`)
  are tested for `to_dict()` / `from_dict()` serialization.
- **Migration tests** — config loading is tested with missing fields to ensure backward
  compatibility when defaults change.
- **Edge cases** — empty inputs, missing files, unknown languages, duplicate onboarding.

## CI Integration

GitHub Actions runs the full test suite on every push to `main` and every pull request:

```yaml
# .github/workflows/ci.yml
name: Tests
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ['3.11', '3.12', '3.13']
    steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v5
    - uses: actions/setup-python@v5
      with:
        python-version: ${{ matrix.python-version }}
    - run: ./scripts/bootstrap.sh
    - run: PYTHONPATH=. python -m pytest tests/ -q
```

The matrix tests against Python 3.11, 3.12, and 3.13 to catch version-specific issues.

## Troubleshooting

### `ModuleNotFoundError: No module named 'bluei'`

You forgot `PYTHONPATH=.`. Either set it explicitly or use `./scripts/test-all.sh` which
handles it automatically.

### `ModuleNotFoundError: No module named 'pytest'`

Run `./scripts/bootstrap.sh` or manually: `uv pip install pytest pyyaml`.

### Tests pick up files in `repos/` or `state/`

The `norecursedirs` in `pytest.ini` should prevent this. If tests are still discovering
files in those directories, ensure you're running from the repo root.

### Flaky test with pattern cache

The global `conftest.py` clears the pattern cache between tests. If you're seeing stale
patterns, ensure the cache-clearing hook is running (check that `conftest.py` is in
`tests/` and not being skipped).

## Quick Reference

| Task | Command |
|------|---------|
| Run all tests | `./scripts/test-all.sh` |
| Run all tests (manual) | `PYTHONPATH=. python -m pytest tests/ -q` |
| Run one file | `PYTHONPATH=. python -m pytest tests/test_config.py -q` |
| Run one test | `PYTHONPATH=. python -m pytest tests/test_runner.py::test_dry_run -q` |
| Run with verbose output | `PYTHONPATH=. python -m pytest tests/ -v` |
| Run and stop on first failure | `PYTHONPATH=. python -m pytest tests/ -x -q` |
| Bootstrap environment | `./scripts/bootstrap.sh` |
| Install deps manually | `uv pip install pytest pyyaml` |
