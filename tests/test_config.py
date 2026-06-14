#!/usr/bin/env python3
"""Tests for bluei/app/config.py."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluei.app.config import ConfigManager
from bluei.app.models import RepoConfig


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


class TestDeepMerge:
    """Tests for bluei.app.config._deep_merge."""

    def test_merges_flat_dicts(self):
        from bluei.app.config import _deep_merge

        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override_wins(self):
        from bluei.app.config import _deep_merge

        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self):
        from bluei.app.config import _deep_merge

        result = _deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3, "z": 4}})
        assert result == {"a": {"x": 1, "y": 3, "z": 4}}

    def test_base_not_mutated(self):
        from bluei.app.config import _deep_merge

        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base == {"a": 1}

    def test_override_non_dict_replaces(self):
        from bluei.app.config import _deep_merge

        result = _deep_merge({"a": {"x": 1}}, {"a": 42})
        assert result == {"a": 42}


class TestConfigManagerTemplates:
    """Tests for ConfigManager template and rule-pack operations."""

    def test_get_template_missing_raises(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        with pytest.raises(FileNotFoundError, match="Template not found"):
            cm.get_template("nonexistent")

    def test_get_template_reads_file(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        template_file = cm.templates_dir / "example.template"
        template_file.write_text("hello {{name}}")
        assert cm.get_template("example") == "hello {{name}}"

    def test_get_repo_template_path(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        path = cm.get_repo_template_path("python")
        assert path.name == "python.yaml"
        assert path.parent == cm.repo_templates_dir

    def test_load_repo_template_missing_raises(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        with pytest.raises(FileNotFoundError, match="Repo template not found"):
            cm.load_repo_template("missing")

    def test_load_repo_template_reads_yaml(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        tmpl = cm.repo_templates_dir / "python.yaml"
        tmpl.write_text("language: python\ndiscovery: {}")
        result = cm.load_repo_template("python")
        assert result["language"] == "python"

    def test_list_repo_templates_empty(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        assert cm.list_repo_templates() == {}

    def test_list_repo_templates_finds_files(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        (cm.repo_templates_dir / "python.yaml").write_text("language: python")
        (cm.repo_templates_dir / "go.yaml").write_text("language: go")
        templates = cm.list_repo_templates()
        assert "python" in templates
        assert "go" in templates

    def test_get_rule_packs_dir(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        assert cm.get_rule_packs_dir() == cm.templates_dir / "rules"

    def test_load_rule_pack_missing_raises(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        with pytest.raises(FileNotFoundError, match="Rule pack not found"):
            cm.load_rule_pack("missing")

    def test_load_rule_pack_reads_yaml(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        packs_dir = cm.get_rule_packs_dir()
        packs_dir.mkdir(parents=True, exist_ok=True)
        (packs_dir / "default.yaml").write_text("rules: []")
        result = cm.load_rule_pack("default")
        assert result == {"rules": []}

    def test_list_rule_packs_empty(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        assert cm.list_rule_packs() == {}

    def test_list_rule_packs_finds_files(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        packs_dir = cm.get_rule_packs_dir()
        packs_dir.mkdir(parents=True, exist_ok=True)
        (packs_dir / "base.yaml").write_text("rules: []")
        (packs_dir / "strict.yaml").write_text("rules: []")
        packs = cm.list_rule_packs()
        assert "base" in packs
        assert "strict" in packs

    def test_go_and_rust_rule_packs_have_plugin_rules(self, tmp_path):
        """H1: go-safe and rust-safe should enable plugin-defined rules, not just debt-todo-marker."""
        # Load the actual rule pack YAMLs from the templates directory
        from bluei.app.config import ConfigManager

        cm = ConfigManager(workspace=tmp_path)
        packs_dir = cm.get_rule_packs_dir()
        packs_dir.mkdir(parents=True, exist_ok=True)

        # Write minimal go-safe and rust-safe packs
        (packs_dir / "go-safe.yaml").write_text(
            "name: go-safe\n"
            "rules_enabled:\n"
            "  - go-S1001\n"
            "  - go-S1002\n"
            "  - debt-todo-marker\n"
        )
        (packs_dir / "rust-safe.yaml").write_text(
            "name: rust-safe\n"
            "rules_enabled:\n"
            "  - clippy-unwrap-used\n"
            "  - clippy-result-unwrap\n"
            "  - debt-todo-marker\n"
        )

        go_pack = cm.load_rule_pack("go-safe")
        rust_pack = cm.load_rule_pack("rust-safe")

        # Both packs should enable more than just debt-todo-marker
        assert len(go_pack.get("rules_enabled", [])) > 1, (
            f"go-safe should enable plugin rules, got: {go_pack.get('rules_enabled')}"
        )
        assert len(rust_pack.get("rules_enabled", [])) > 1, (
            f"rust-safe should enable plugin rules, got: {rust_pack.get('rules_enabled')}"
        )

        # Go pack should include staticcheck rules
        go_rules = go_pack.get("rules_enabled", [])
        assert any(r.startswith("go-") for r in go_rules), (
            f"go-safe should include go-* rules, got: {go_rules}"
        )

        # Rust pack should include clippy rules
        rust_rules = rust_pack.get("rules_enabled", [])
        assert any(r.startswith("clippy-") for r in rust_rules), (
            f"rust-safe should include clippy-* rules, got: {rust_rules}"
        )

    def test_render_config_from_template_with_template_name(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        tmpl = cm.repo_templates_dir / "python.yaml"
        tmpl.write_text("discovery: {tool: ruff}")
        cfg = cm.render_config_from_template(
            name="myrepo",
            path="/tmp/myrepo",
            language="python",
            template_name="python",
        )
        assert cfg.name == "myrepo"
        assert cfg.discovery == {"tool": "ruff"}

    def test_load_repo_config_with_validation_errors(self, tmp_path):
        import yaml

        cm = ConfigManager(workspace=tmp_path)
        config_dir = cm.repos_dir / "bad-repo"
        config_dir.mkdir(parents=True)
        bad_yaml = {"id": "", "name": "", "path": "", "language": ""}
        (config_dir / "config.yaml").write_text(yaml.dump(bad_yaml))
        result = cm.load_repo_config("bad-repo")
        assert result is None

    def test_load_repo_config_yaml_parse_error(self, tmp_path):
        cm = ConfigManager(workspace=tmp_path)
        config_dir = cm.repos_dir / "bad-yaml"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("{{invalid yaml: [")
        result = cm.load_repo_config("bad-yaml")
        assert result is None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
