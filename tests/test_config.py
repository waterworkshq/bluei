#!/usr/bin/env python3
"""Tests for qa_agent/config.py."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.config import ConfigManager
from qa_agent.models import RepoConfig


class TestConfigManager:
    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cm = ConfigManager(workspace=self.tmp)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_repo_config_path(self):
        path = self.cm.get_repo_config_path("my-repo")
        assert path.name == "config.yaml"
        assert path.parent.name == "my-repo"

    def test_save_and_load_repo_config(self):
        config = RepoConfig(
            id="repo-test",
            name="test-repo",
            path="/tmp/test",
            language="python",
            enabled=True,
        )
        saved = self.cm.save_repo_config(config)
        assert saved.exists()

        loaded = self.cm.load_repo_config("test-repo")
        assert loaded is not None
        assert loaded.name == "test-repo"
        assert loaded.language == "python"

    def test_load_repo_config_missing(self):
        result = self.cm.load_repo_config("nonexistent")
        assert result is None

    def test_list_repo_configs_empty(self):
        configs = self.cm.list_repo_configs()
        assert configs == {}

    def test_list_repo_configs(self):
        config = RepoConfig(
            id="repo-ky",
            name="ky",
            path="/home/test/ky",
            language="typescript",
        )
        self.cm.save_repo_config(config)

        configs = self.cm.list_repo_configs()
        assert "ky" in configs
        assert configs["ky"].name == "config.yaml"

    def test_render_config_from_template(self):
        # No template file needed for basic render
        cfg = self.cm.render_config_from_template(
            name="test-repo",
            path="/tmp/test",
            language="rust",
        )
        assert cfg.name == "test-repo"
        assert cfg.language == "rust"
        assert cfg.id == "repo-test-repo"
        assert cfg.meta["inferred_by"] == "template"

    def test_render_config_with_extras(self):
        cfg = self.cm.render_config_from_template(
            name="extra-repo",
            path="/tmp/extra",
            language="go",
            fix_engine="claude",
            enabled=False,
        )
        assert cfg.fix_engine == "claude"
        assert cfg.enabled is False

    def test_templates_dir_workspace_relative(self):
        # templates dir should be inside workspace
        assert self.cm.templates_dir.parent == self.cm.workspace

    def test_repo_templates_dir_initially_empty_list(self):
        templates = self.cm.list_repo_templates()
        assert isinstance(templates, dict)


class TestRepoConfigMigration:
    """Test that configs without safety/meta fields get safe defaults on load."""

    def setup_method(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cm = ConfigManager(workspace=self.tmp)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_from_dict_sets_safety_defaults(self):
        # Raw dict without explicit safety
        data = {
            "id": "repo-legacy",
            "name": "legacy",
            "path": "/legacy",
            "language": "python",
            "enabled": True,
            "github": {"live_actions": True},
        }
        cfg = RepoConfig.from_dict(data)
        assert cfg.safety is not None
        assert cfg.safety.get("mode") == "merge"
        assert cfg.safety.get("require_clean_worktree") is True

    def test_from_dict_preserves_explicit_safety(self):
        data = {
            "id": "repo-explicit",
            "name": "explicit",
            "path": "/explicit",
            "language": "python",
            "enabled": True,
            "safety": {
                "mode": "pr",
                "profile": "aggressive",
                "require_clean_worktree": False,
            },
        }
        cfg = RepoConfig.from_dict(data)
        assert cfg.safety.get("mode") == "pr"
        assert cfg.safety.get("require_clean_worktree") is False

    def test_from_dict_sets_meta_defaults(self):
        data = {
            "id": "repo-nometa",
            "name": "no-meta",
            "path": "/nometa",
            "language": "python",
            "enabled": True,
        }
        cfg = RepoConfig.from_dict(data)
        assert cfg.meta is not None
        assert cfg.meta.get("inferred_by") in ("legacy", "migration")

    def test_from_dict_sets_review_care_defaults(self):
        data = {
            "id": "repo-noreviewcare",
            "name": "no-reviewcare",
            "path": "/noreviewcare",
            "language": "python",
            "enabled": True,
        }
        cfg = RepoConfig.from_dict(data)
        assert cfg.review_care is not None
        assert cfg.review_care.get("enabled") is True
        assert cfg.review_care.get("mode") == "observation"

    def test_from_dict_adds_missing_review_mode(self):
        data = {
            "id": "repo-legacy-reviewcare",
            "name": "legacy-reviewcare",
            "path": "/legacy-reviewcare",
            "language": "python",
            "enabled": True,
            "review_care": {
                "enabled": True,
                "provider_order": ["github"],
                "max_attempts": 4,
            },
        }
        cfg = RepoConfig.from_dict(data)
        assert cfg.review_care.get("mode") == "observation"
        assert cfg.review_care.get("max_attempts") == 4

    def test_explicit_review_mode_round_trips(self):
        cfg = RepoConfig(
            id="repo-autonomous",
            name="autonomous",
            path="/autonomous",
            language="python",
            review_care={
                "enabled": True,
                "mode": "autonomous-review",
                "provider_order": ["github"],
            },
        )
        saved = self.cm.save_repo_config(cfg)
        assert saved.exists()

        loaded = self.cm.load_repo_config("autonomous")
        assert loaded is not None
        assert loaded.review_care.get("mode") == "autonomous-review"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
