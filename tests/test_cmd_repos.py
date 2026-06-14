"""Tests for the 'bluei repos' command."""

from pathlib import Path
from unittest.mock import patch, MagicMock


def _make_repo(name, language="python", enabled=True, path="/tmp/repo"):
    repo = MagicMock()
    repo.config.name = name
    repo.config.language = language
    repo.config.enabled = enabled
    repo.config.path = Path(path)
    return repo


def test_repos_no_projects(capsys):
    """Should print 'no projects registered' when registry is empty."""
    with patch("bluei.app.registry.RepoRegistry") as mock_reg:
        mock_reg.return_value.list_all.return_value = []
        from bin.bluei import _cmd_repos

        result = _cmd_repos([])
        assert result == 0
        captured = capsys.readouterr()
        assert "no projects registered" in captured.out


def test_repos_lists_enabled_by_default(capsys):
    """Should list enabled repos and exclude disabled by default."""
    with patch("bluei.app.registry.RepoRegistry") as mock_reg:
        mock_reg.return_value.list_all.return_value = [
            _make_repo("api-server", "python", enabled=True),
            _make_repo("frontend", "typescript", enabled=True),
            _make_repo("legacy", "python", enabled=False),
        ]
        from bin.bluei import _cmd_repos

        result = _cmd_repos([])
        assert result == 0
        captured = capsys.readouterr()
        assert "api-server" in captured.out
        assert "frontend" in captured.out
        assert "legacy" not in captured.out


def test_repos_all_includes_disabled(capsys):
    """--all flag should include disabled repos."""
    with patch("bluei.app.registry.RepoRegistry") as mock_reg:
        mock_reg.return_value.list_all.return_value = [
            _make_repo("api-server", "python", enabled=True),
            _make_repo("legacy", "python", enabled=False),
        ]
        from bin.bluei import _cmd_repos

        result = _cmd_repos(["--all"])
        assert result == 0
        captured = capsys.readouterr()
        assert "api-server" in captured.out
        assert "legacy" in captured.out


def test_repos_shows_language_and_path(capsys):
    """Output should include language and path columns."""
    with patch("bluei.app.registry.RepoRegistry") as mock_reg:
        mock_reg.return_value.list_all.return_value = [
            _make_repo("svc", "go", enabled=True, path="/opt/svc"),
        ]
        from bin.bluei import _cmd_repos

        result = _cmd_repos([])
        assert result == 0
        captured = capsys.readouterr()
        assert "go" in captured.out
        assert "/opt/svc" in captured.out
