#!/usr/bin/env python3
"""Tests for Repo Registry."""

import pytest
from pathlib import Path
from unittest.mock import patch
import tempfile
import shutil

from bluei.app.models import RepoConfig, RepoStatus
from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "qa-agent"
        workspace.mkdir()
        (workspace / "repos").mkdir()
        (workspace / "plugins").mkdir()
        (workspace / "logs").mkdir()
        yield workspace


@pytest.fixture
def config_manager(temp_workspace):
    return ConfigManager(temp_workspace)


@pytest.fixture
def registry(config_manager):
    return RepoRegistry(config_manager)


class TestRepoRegistry:
    """Test repo registry operations."""

    def test_create_repo(self, registry):
        """Test creating a repo."""
        config = RepoConfig(
            id="test-001", name="test-repo", path="/tmp/test", language="python"
        )
        repo = registry.create(config)

        assert repo is not None
        assert repo.config.name == "test-repo"
        assert repo.config.language == "python"
        assert repo.status == RepoStatus.IDLE

    def test_read_repo(self, registry):
        """Test reading a repo."""
        config = RepoConfig(
            id="test-002",
            name="read-test-repo",
            path="/tmp/read-test",
            language="typescript",
        )
        registry.create(config)

        repo = registry.read("read-test-repo")

        assert repo is not None
        assert repo.config.name == "read-test-repo"
        assert repo.config.language == "typescript"

    def test_read_nonexistent_repo(self, registry):
        """Test reading a repo that doesn't exist."""
        repo = registry.read("nonexistent")
        assert repo is None

    def test_update_repo(self, registry):
        """Test updating repo state."""
        config = RepoConfig(
            id="test-003",
            name="update-test-repo",
            path="/tmp/update-test",
            language="python",
        )
        registry.create(config)

        # Update state
        updated = registry.update(
            "update-test-repo",
            {
                "status": RepoStatus.READY,
                "current_health_score": 75.0,
            },
        )

        assert updated is not None
        assert updated.status == RepoStatus.READY
        assert updated.current_health_score == 75.0

    def test_list_repos(self, registry):
        """Test listing all repos."""
        config1 = RepoConfig(id="r1", name="repo1", path="/tmp/r1", language="python")
        config2 = RepoConfig(
            id="r2", name="repo2", path="/tmp/r2", language="typescript"
        )

        registry.create(config1)
        registry.create(config2)

        repos = registry.list_all()

        assert len(repos) == 2
        names = [r.config.name for r in repos]
        assert "repo1" in names
        assert "repo2" in names

    def test_list_enabled_repos(self, registry):
        """Test listing enabled repos."""
        config1 = RepoConfig(
            id="e1",
            name="enabled-repo",
            path="/tmp/e1",
            language="python",
            enabled=True,
        )
        config2 = RepoConfig(
            id="e2",
            name="disabled-repo",
            path="/tmp/e2",
            language="python",
            enabled=False,
        )

        registry.create(config1)
        registry.create(config2)

        repos = registry.list_enabled()

        assert len(repos) == 1
        assert repos[0].config.name == "enabled-repo"

    def test_find_by_name(self, registry):
        """Test finding repo by name."""
        config = RepoConfig(
            id="test-004", name="find-by-name", path="/tmp/find-name", language="go"
        )
        registry.create(config)

        repo = registry.find_by_name("find-by-name")

        assert repo is not None
        assert repo.config.name == "find-by-name"

    def test_find_by_path(self, registry):
        """Test finding repo by path."""
        config = RepoConfig(
            id="test-005", name="find-by-path", path="/tmp/find-path", language="rust"
        )
        registry.create(config)

        repo = registry.find_by_path(Path("/tmp/find-path"))

        assert repo is not None
        assert repo.config.name == "find-by-path"

    def test_update_repo_recovers_from_invalid_state_file(self, registry):
        config = RepoConfig(
            id="test-007",
            name="invalid-state-repo",
            path="/tmp/invalid-state",
            language="python",
        )
        registry.create(config)

        state_file = (
            registry.config.repos_dir
            / "invalid-state-repo"
            / "state"
            / "repo_state.json"
        )
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("")

        updated = registry.update(
            "invalid-state-repo",
            {
                "status": RepoStatus.RUNNING,
                "current_findings_count": 3,
            },
        )

        assert updated is not None
        assert updated.status == RepoStatus.RUNNING
        assert updated.current_findings_count == 3

    def test_delete_repo(self, registry):
        """Test deleting a repo."""
        config = RepoConfig(
            id="test-006", name="delete-test", path="/tmp/delete", language="python"
        )
        registry.create(config)

        # Verify it exists
        repo = registry.read("delete-test")
        assert repo is not None

        # Delete
        result = registry.delete("delete-test")
        assert result == True

        # Verify it's gone
        repo = registry.read("delete-test")
        assert repo is None

    def test_delete_removes_entire_repo_directory(self, registry):
        repo_dir = registry.config.repos_dir / "ghost-repo"

        config = RepoConfig(
            id="test-ghost",
            name="ghost-repo",
            path="/tmp/ghost",
            language="python",
        )
        registry.create(config)

        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "repo_state.json").write_text('{"status": "ready"}')
        (state_dir / "findings.jsonl").write_text('{"id": "f1"}\n')

        runs_dir = repo_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "run-001.json").write_text('{"ok": true}')

        baselines_dir = repo_dir / "baselines"
        baselines_dir.mkdir(parents=True, exist_ok=True)
        (baselines_dir / "baseline.json").write_text("{}")

        worktrees_dir = repo_dir / "worktrees"
        worktrees_dir.mkdir(parents=True, exist_ok=True)
        (worktrees_dir / "wt-001").mkdir()

        assert repo_dir.exists()
        assert (state_dir / "repo_state.json").exists()
        assert (state_dir / "findings.jsonl").exists()
        assert (runs_dir / "run-001.json").exists()
        assert (baselines_dir / "baseline.json").exists()
        assert (worktrees_dir / "wt-001").exists()

        result = registry.delete("ghost-repo")
        assert result is True

        assert not repo_dir.exists(), (
            f"repo directory {repo_dir} should be fully removed"
        )

        config2 = RepoConfig(
            id="test-ghost-v2",
            name="ghost-repo",
            path="/tmp/ghost-v2",
            language="python",
        )
        registry.create(config2)

        new_state_dir = repo_dir / "state"
        assert not (new_state_dir / "repo_state.json").exists()
        assert not (new_state_dir / "findings.jsonl").exists()
        assert not (repo_dir / "runs").exists()
        assert not (repo_dir / "baselines").exists()
        assert not (repo_dir / "worktrees").exists()

    def test_delete_nonexistent_repo_is_safe(self, registry):
        result = registry.delete("no-such-repo")
        assert result is False


# ── Extracted from test_app_small_modules_remaining.py ──
# Additional registry edge cases


class TestRepoRegistryCreateFromPath:
    def test_create_from_path_basic(self, registry, tmp_path):
        repo_dir = tmp_path / "my-repo"
        repo_dir.mkdir()
        (repo_dir / "main.py").write_text("print('hello')\n")
        repo = registry.create_from_path(repo_dir)
        assert repo.config.name == "my-repo"
        assert repo.config.language == "python"

    def test_create_from_path_custom_name_and_language(self, registry, tmp_path):
        repo_dir = tmp_path / "some-dir"
        repo_dir.mkdir()
        repo = registry.create_from_path(repo_dir, name="custom-name", language="go")
        assert repo.config.name == "custom-name"
        assert repo.config.language == "go"

    def test_create_from_path_duplicate_path_raises(self, registry, tmp_path):
        repo_dir = tmp_path / "dup-repo"
        repo_dir.mkdir()
        registry.create_from_path(repo_dir)
        with pytest.raises(ValueError, match="already registered at this path"):
            registry.create_from_path(repo_dir)

    def test_create_from_path_duplicate_name_raises(self, registry, tmp_path):
        dir_a = tmp_path / "dir-a"
        dir_b = tmp_path / "dir-b"
        dir_a.mkdir()
        dir_b.mkdir()
        registry.create_from_path(dir_a, name="shared-name")
        with pytest.raises(ValueError, match="already registered with name"):
            registry.create_from_path(dir_b, name="shared-name")

    def test_create_from_path_with_mode(self, registry, tmp_path):
        repo_dir = tmp_path / "mode-repo"
        repo_dir.mkdir()
        repo = registry.create_from_path(repo_dir, mode="fix")
        assert repo.config.safety["mode"] == "fix"

    def test_create_from_path_disabled(self, registry, tmp_path):
        repo_dir = tmp_path / "disabled-repo"
        repo_dir.mkdir()
        repo = registry.create_from_path(repo_dir, enabled=False)
        assert repo.config.enabled is False


class TestDetectLanguage:
    def test_detect_python(self, tmp_path):
        (tmp_path / "app.py").write_text("pass\n")
        assert RepoRegistry._detect_language(tmp_path) == "python"

    def test_detect_typescript(self, tmp_path):
        (tmp_path / "app.ts").write_text("export {};\n")
        assert RepoRegistry._detect_language(tmp_path) == "typescript"

    def test_detect_typescript_tsx(self, tmp_path):
        (tmp_path / "component.tsx").write_text("export {};\n")
        assert RepoRegistry._detect_language(tmp_path) == "typescript"

    def test_detect_javascript(self, tmp_path):
        (tmp_path / "index.js").write_text("module.exports = {};\n")
        assert RepoRegistry._detect_language(tmp_path) == "javascript"

    def test_detect_javascript_jsx(self, tmp_path):
        (tmp_path / "view.jsx").write_text("export default () => {};\n")
        assert RepoRegistry._detect_language(tmp_path) == "javascript"

    def test_detect_by_marker_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("")
        assert RepoRegistry._detect_language(tmp_path) == "python"

    def test_detect_by_marker_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        assert RepoRegistry._detect_language(tmp_path) == "python"

    def test_detect_by_marker_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("pytest\n")
        assert RepoRegistry._detect_language(tmp_path) == "python"

    def test_detect_by_marker_pipfile(self, tmp_path):
        (tmp_path / "Pipfile").write_text("[packages]\n")
        assert RepoRegistry._detect_language(tmp_path) == "python"

    def test_detect_by_marker_tsconfig(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        assert RepoRegistry._detect_language(tmp_path) == "typescript"

    def test_detect_by_marker_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert RepoRegistry._detect_language(tmp_path) == "javascript"

    def test_detect_by_marker_eslintrc(self, tmp_path):
        (tmp_path / ".eslintrc").write_text("{}")
        assert RepoRegistry._detect_language(tmp_path) == "javascript"

    def test_detect_unknown(self, tmp_path):
        (tmp_path / "README.md").write_text("# hello")
        assert RepoRegistry._detect_language(tmp_path) == "unknown"

    def test_detect_permission_error(self, tmp_path):
        with patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            assert RepoRegistry._detect_language(tmp_path) == "unknown"


class TestRegistryLoadRegistryNoFile:
    def test_load_registry_returns_default_when_file_deleted(self, registry):
        registry.registry_file.unlink()
        data = registry._load_registry()
        assert data == {"repos": [], "version": "1.0"}


class TestRegistryUpdateEdgeCases:
    def test_update_nonexistent_repo_returns_none(self, registry):
        result = registry.update("no-such-repo", {"status": RepoStatus.RUNNING})
        assert result is None

    def test_update_config_attributes(self, registry):
        config = RepoConfig(
            id="test-cfg-update",
            name="cfg-update-repo",
            path="/tmp/cfg-update",
            language="python",
        )
        registry.create(config)
        updated = registry.update("cfg-update-repo", {"enabled": False})
        assert updated is not None
        assert updated.config.enabled is False

    def test_update_status_as_string(self, registry):
        config = RepoConfig(
            id="test-str-status",
            name="str-status-repo",
            path="/tmp/str-status",
            language="python",
        )
        registry.create(config)
        updated = registry.update("str-status-repo", {"status": "running"})
        assert updated is not None
        assert updated.status == RepoStatus.RUNNING

    def test_update_without_status_preserves_existing(self, registry):
        config = RepoConfig(
            id="test-no-status",
            name="no-status-repo",
            path="/tmp/no-status",
            language="python",
        )
        registry.create(config)
        registry.update("no-status-repo", {"status": RepoStatus.READY})
        updated = registry.update("no-status-repo", {"total_fixes": 5})
        assert updated is not None
        assert updated.status == RepoStatus.READY
        assert updated.total_fixes == 5


class TestRegistryFindByPath:
    def test_find_by_path_returns_none_when_not_found(self, registry, tmp_path):
        result = registry.find_by_path(tmp_path / "nonexistent")
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
