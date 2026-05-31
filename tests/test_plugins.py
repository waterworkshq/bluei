#!/usr/bin/env python3
"""Pytest-native tests for Plugin System."""

from pathlib import Path
import shutil

import pytest

from bluei.app.plugins import PluginLoader
from bluei.app.health import HealthEngine


@pytest.fixture
def plugins_dir():
    return Path(__file__).resolve().parents[1] / "plugins"


@pytest.fixture
def loader(plugins_dir):
    return PluginLoader(plugins_dir)


@pytest.fixture
def temp_repo(tmp_path):
    return tmp_path / "repo"


def test_discover_plugins(loader):
    manifests = loader.discover()
    plugin_ids = [m.get("id") for m in manifests]
    assert "plugin-test" in plugin_ids


def test_load_plugin(loader):
    plugin = loader.load("plugin-test")
    assert plugin is not None
    assert plugin.id == "plugin-test"
    assert plugin.name == "Test Plugin"
    assert "test" in plugin.languages


def test_get_plugin(loader):
    loader.load("plugin-test")
    plugin = loader.get("plugin-test")
    assert plugin is not None
    assert plugin.id == "plugin-test"


def test_get_for_language(loader):
    plugin = loader.get_for_language("test")
    assert plugin is not None
    assert plugin.id == "plugin-test"


def test_list_loaded(loader):
    loader.load("plugin-test")
    loaded = loader.list_loaded()
    assert "plugin-test" in loaded


def test_get_manifest(loader):
    loader.discover()
    manifest = loader.get_manifest("plugin-test")
    assert manifest is not None
    assert manifest["id"] == "plugin-test"
    assert manifest["name"] == "Test Plugin"


def test_plugin_discover_method(loader, temp_repo):
    temp_repo.mkdir()
    (temp_repo / "test.txt").write_text("test content")
    plugin = loader.load("plugin-test")
    findings = plugin.discover(temp_repo, {})
    assert len(findings) > 0
    assert findings[0].rule == "test-rule"


def test_plugin_detect_method(loader, tmp_path):
    matching_repo = tmp_path / "matching-repo"
    matching_repo.mkdir()
    (matching_repo / "test.txt").write_text("test content")

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()

    plugin = loader.load("plugin-test")
    assert plugin.detect(matching_repo) is True
    assert plugin.detect(other_repo) is False


def test_language_pack_manifest_rules_register_health_mappings(loader):
    manifests = loader.discover()
    rust = next(m for m in manifests if m["id"] == "plugin-rust")

    rule_ids = [rule["id"] for rule in rust["discovery"]["rules"]]

    assert "clippy-unwrap-used" in rule_ids
    assert (
        rust["health_mapping"]["rule_to_component"]["clippy-unwrap-used"]
        == "bug_quality"
    )
    assert HealthEngine.RULE_TO_COMPONENT["clippy-unwrap-used"] == "bug_quality"


def test_language_pack_detector_catalog_includes_expanded_languages(loader):
    catalog = loader.detector_catalog()
    rules = {entry["rule"]: entry for entry in catalog}

    assert rules["clippy-unwrap-used"]["language"] == "rust"
    assert rules["go-SA1000"]["category"] == "bug"
    assert rules["shell-SC2086"]["autofix"] is True
    assert rules["docker-DL3006"]["language"] == "dockerfile"
    assert rules["md-MD009"]["category"] == "lint"


# ---------------------------------------------------------------------------
# ToolWrapperPlugin tests — tool execution, detection, fallback behavior
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch
import subprocess

from bluei.app.plugins import ToolWrapperPlugin


class SimpleToolPlugin(ToolWrapperPlugin):
    """Concrete subclass for testing ToolWrapperPlugin behavior."""

    @property
    def id(self) -> str:
        return "tool-test"

    @property
    def name(self) -> str:
        return "Tool Test"

    @property
    def languages(self):
        return ["python"]

    @property
    def rules(self):
        return ["fake-rule"]

    def parse_output(self, output, repo_path):
        from bluei.engine.models import Finding

        return [
            Finding(
                finding_id="tool-finding-1",
                repo=str(repo_path),
                path="main.py",
                line=1,
                rule="fake-rule",
                snippet="bad code",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ]


class TestToolWrapperPlugin:
    @pytest.fixture
    def plugin(self):
        return SimpleToolPlugin(
            manifest={
                "discovery": {
                    "tool": "echo",
                    "tool_args": ["hello"],
                },
                "detection": {
                    "files": ["*.py"],
                },
            }
        )

    def test_tool_name_from_manifest(self, plugin):
        assert plugin.tool_name == "echo"

    def test_tool_args_from_manifest(self, plugin):
        assert plugin.tool_args == ["hello"]

    def test_run_tool_returns_stdout(self, plugin, tmp_path):
        result = plugin._run_tool(tmp_path)
        assert result is not None
        assert "hello" in result

    def test_run_tool_returns_none_when_tool_missing(self, tmp_path):
        p = SimpleToolPlugin(manifest={"discovery": {"tool": "nonexistent_tool_xyz"}})
        result = p._run_tool(tmp_path)
        assert result is None

    def test_discover_returns_findings_when_tool_works(self, plugin, tmp_path):
        findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1
        assert findings[0].rule == "fake-rule"

    def test_discover_returns_empty_when_tool_missing(self, tmp_path):
        p = SimpleToolPlugin(manifest={"discovery": {"tool": "nonexistent_xyz"}})
        findings = p.discover(tmp_path, {})
        assert findings == []

    def test_detect_matches_files(self, plugin, tmp_path):
        (tmp_path / "app.py").write_text("x = 1")
        assert plugin.detect(tmp_path) is True

    def test_detect_no_match(self, plugin, tmp_path):
        (tmp_path / "app.go").write_text("package main")
        assert plugin.detect(tmp_path) is False

    def test_detect_empty_patterns(self, tmp_path):
        p = SimpleToolPlugin(manifest={"detection": {"files": []}})
        assert p.detect(tmp_path) is False

    def test_run_tool_timeout(self, plugin, tmp_path):
        with patch("bluei.app.plugins.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=120)
            result = plugin._run_tool(tmp_path)
        assert result is None

    def test_run_tool_file_not_found(self, plugin, tmp_path):
        with patch("bluei.app.plugins.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("no tool")
            result = plugin._run_tool(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# PluginLoader edge cases — broken plugins, missing files, empty dirs
# ---------------------------------------------------------------------------


class TestPluginLoaderEdgeCases:
    def test_discover_nonexistent_dir(self, tmp_path):
        loader = PluginLoader(tmp_path / "nope")
        manifests = loader.discover()
        assert manifests == []

    def test_load_unknown_plugin(self, tmp_path):
        loader = PluginLoader(tmp_path)
        result = loader.load("nonexistent")
        assert result is None

    def test_load_plugin_with_missing_py_file(self, tmp_path):
        plugin_dir = tmp_path / "broken"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("id: broken-plugin\nname: Broken")
        loader = PluginLoader(tmp_path)
        loader.discover()
        result = loader.load("broken-plugin")
        assert result is None

    def test_load_plugin_with_bad_python(self, tmp_path):
        plugin_dir = tmp_path / "bad"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("id: bad-plugin\nname: Bad")
        (plugin_dir / "plugin.py").write_text("raise RuntimeError('cannot load')\n")
        loader = PluginLoader(tmp_path)
        loader.discover()
        result = loader.load("bad-plugin")
        assert result is None

    def test_load_plugin_without_plugin_class(self, tmp_path):
        plugin_dir = tmp_path / "noclass"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("id: noclass-plugin\nname: NoClass")
        (plugin_dir / "plugin.py").write_text("x = 42\n")
        loader = PluginLoader(tmp_path)
        loader.discover()
        result = loader.load("noclass-plugin")
        assert result is None

    def test_load_all_with_no_plugins(self, tmp_path):
        loader = PluginLoader(tmp_path / "empty")
        plugins = loader.load_all()
        assert plugins == {}

    def test_list_available_empty_dir(self, tmp_path):
        loader = PluginLoader(tmp_path / "missing")
        available = loader.list_available()
        assert available == []

    def test_get_for_language_no_match(self, tmp_path):
        loader = PluginLoader(tmp_path)
        result = loader.get_for_language("brainfuck")
        assert result is None

    def test_get_nonexistent(self, tmp_path):
        loader = PluginLoader(tmp_path)
        assert loader.get("nope") is None

    def test_load_all_caches_plugins(self, tmp_path):
        """Calling load_all() twice should not re-discover."""
        plugin_dir = tmp_path / "myplug"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(
            "id: plugin-myplug\nname: MyPlug\nlanguages:\n  - test\n"
        )
        (plugin_dir / "plugin.py").write_text(
            "from bluei.app.plugins import DiscoveryPlugin\n"
            "from bluei.engine.models import Finding\n"
            "class Plugin(DiscoveryPlugin):\n"
            "    id = 'myplug'\n"
            "    name = 'MyPlug'\n"
            "    languages = ['test']\n"
            "    rules = ['r1']\n"
            "    def discover(self, p, c): return []\n"
            "    def detect(self, p): return True\n"
        )
        loader = PluginLoader(tmp_path)
        first = loader.load_all()
        second = loader.load_all()
        assert "plugin-myplug" in first
        assert first is second  # same dict object (cached)

    def test_discover_skips_non_directory_entries(self, tmp_path):
        (tmp_path / "random.txt").write_text("not a plugin dir")
        loader = PluginLoader(tmp_path)
        manifests = loader.discover()
        assert manifests == []

    def test_discover_handles_bad_yaml(self, tmp_path):
        plugin_dir = tmp_path / "bad-yaml"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("{{invalid yaml!!")
        loader = PluginLoader(tmp_path)
        manifests = loader.discover()
        # Should not crash; manifest is skipped
        assert manifests == []

    def test_health_mapping_missing_file(self, tmp_path):
        loader = PluginLoader(tmp_path)
        mapping = loader._load_health_mapping(tmp_path / "nope")
        assert mapping == {"rule_to_component": {}, "category_inference": {}}

    def test_health_mapping_valid_file(self, tmp_path):
        (tmp_path / "health_mapping.yaml").write_text(
            "rule_to_component:\n  r1: quality\ncategory_inference:\n  lint: style\n"
        )
        loader = PluginLoader(tmp_path)
        mapping = loader._load_health_mapping(tmp_path)
        assert mapping["rule_to_component"]["r1"] == "quality"
        assert mapping["category_inference"]["lint"] == "style"
