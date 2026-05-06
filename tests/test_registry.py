#!/usr/bin/env python3
"""Tests for Repo Registry."""

import pytest
from pathlib import Path
import tempfile
import shutil

from qa_agent.models import RepoConfig, RepoStatus
from qa_agent.config import ConfigManager
from qa_agent.registry import RepoRegistry


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / 'qa-agent'
        workspace.mkdir()
        (workspace / 'repos').mkdir()
        (workspace / 'plugins').mkdir()
        (workspace / 'logs').mkdir()
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
            id="test-001",
            name="test-repo",
            path="/tmp/test",
            language="python"
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
            language="typescript"
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
            language="python"
        )
        registry.create(config)
        
        # Update state
        updated = registry.update("update-test-repo", {
            'status': RepoStatus.READY,
            'current_health_score': 75.0,
        })
        
        assert updated is not None
        assert updated.status == RepoStatus.READY
        assert updated.current_health_score == 75.0
    
    def test_list_repos(self, registry):
        """Test listing all repos."""
        config1 = RepoConfig(id="r1", name="repo1", path="/tmp/r1", language="python")
        config2 = RepoConfig(id="r2", name="repo2", path="/tmp/r2", language="typescript")
        
        registry.create(config1)
        registry.create(config2)
        
        repos = registry.list_all()
        
        assert len(repos) == 2
        names = [r.config.name for r in repos]
        assert "repo1" in names
        assert "repo2" in names
    
    def test_list_enabled_repos(self, registry):
        """Test listing enabled repos."""
        config1 = RepoConfig(id="e1", name="enabled-repo", path="/tmp/e1", language="python", enabled=True)
        config2 = RepoConfig(id="e2", name="disabled-repo", path="/tmp/e2", language="python", enabled=False)
        
        registry.create(config1)
        registry.create(config2)
        
        repos = registry.list_enabled()
        
        assert len(repos) == 1
        assert repos[0].config.name == "enabled-repo"
    
    def test_find_by_name(self, registry):
        """Test finding repo by name."""
        config = RepoConfig(
            id="test-004",
            name="find-by-name",
            path="/tmp/find-name",
            language="go"
        )
        registry.create(config)
        
        repo = registry.find_by_name("find-by-name")
        
        assert repo is not None
        assert repo.config.name == "find-by-name"
    
    def test_find_by_path(self, registry):
        """Test finding repo by path."""
        config = RepoConfig(
            id="test-005",
            name="find-by-path",
            path="/tmp/find-path",
            language="rust"
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
            language="python"
        )
        registry.create(config)

        state_file = registry.config.repos_dir / "invalid-state-repo" / "state" / "repo_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("")

        updated = registry.update("invalid-state-repo", {
            'status': RepoStatus.RUNNING,
            'current_findings_count': 3,
        })

        assert updated is not None
        assert updated.status == RepoStatus.RUNNING
        assert updated.current_findings_count == 3

    def test_delete_repo(self, registry):
        """Test deleting a repo."""
        config = RepoConfig(
            id="test-006",
            name="delete-test",
            path="/tmp/delete",
            language="python"
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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
