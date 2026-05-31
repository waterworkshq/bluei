import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bluei.engine.models import Finding


def pytest_runtest_setup(item):
    try:
        from bluei.engine.pattern_replay import _clear_pattern_cache

        _clear_pattern_cache()
    except Exception:
        pass


@pytest.fixture
def make_finding():
    """Factory fixture: returns a callable that creates Finding instances with sensible defaults.

    Usage in tests::

        def test_something(make_finding):
            f = make_finding(rule="broad-except", path="src/app.py")
            assert f.rule == "broad-except"
    """

    def _make_finding(**overrides):
        defaults = {
            "finding_id": "f001",
            "repo": "test-repo",
            "path": "src/main.py",
            "line": 42,
            "rule": "ruff-c408",
            "snippet": "dict(a=1)",
            "confidence": 0.72,
            "quick_win": True,
            "safe_to_autofix": True,
        }
        defaults.update(overrides)
        return Finding(**defaults)

    return _make_finding


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo (init + config + initial commit). Returns the repo Path."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    for args in [
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ]:
        subprocess.run(args, cwd=str(repo_path), capture_output=True, check=True)
    (repo_path / "README").write_text("init")
    subprocess.run(
        ["git", "add", "."], cwd=str(repo_path), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
    )
    return repo_path


@pytest.fixture
def git_commit_all():
    """Factory fixture: returns a function(repo_path, message="init") that stages all and commits."""

    def _git_commit_all(repo_path: Path, message: str = "init"):
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )

    return _git_commit_all


@pytest.fixture
def make_app_finding():
    """Factory fixture: returns a callable that creates unified Finding instances.

    Supports all fields including severity, category, discovered_at.
    """

    def _make_app_finding(**overrides):
        defaults = {
            "finding_id": "f001",
            "repo": "test-repo",
            "path": "src/main.py",
            "line": 42,
            "rule": "ruff-c408",
            "snippet": "dict(a=1)",
            "confidence": 0.72,
            "quick_win": True,
            "safe_to_autofix": True,
        }
        defaults.update(overrides)
        return Finding(**defaults)

    return _make_app_finding


@pytest.fixture
def mock_smtp_server():
    """A MagicMock pre-configured as an SMTP context manager."""
    server = MagicMock()
    server.__enter__ = lambda s: s
    server.__exit__ = MagicMock(return_value=False)
    return server
