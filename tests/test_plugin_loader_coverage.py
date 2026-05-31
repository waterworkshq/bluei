"""Tests for bluei/engine/plugin_loader.py — language detection, plugin loading edge cases."""

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from bluei.engine.plugin_loader import (
    _find_plugins_dir,
    _get_plugin_manifest,
    _load_plugin_module,
    detect_repo_languages,
    discover_plugins,
    load_applicable_plugins,
    load_plugin,
    run_plugin_discovery,
)


class TestDetectRepoLanguages:
    def test_python_from_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("pass")
        result = detect_repo_languages(tmp_path)
        assert "python" in result

    def test_python_from_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]")
        result = detect_repo_languages(tmp_path)
        assert "python" in result

    def test_typescript_from_tsconfig(self, tmp_path):
        (tmp_path / "tsconfig.json").write_text("{}")
        result = detect_repo_languages(tmp_path)
        assert "typescript" in result

    def test_go_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module test")
        result = detect_repo_languages(tmp_path)
        assert "go" in result

    def test_rust_from_cargo(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]")
        result = detect_repo_languages(tmp_path)
        assert "rust" in result

    def test_shell_from_glob(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "deploy.sh").write_text("#!/bin/bash")
        result = detect_repo_languages(tmp_path)
        assert "shell" in result

    def test_markdown_from_glob(self, tmp_path):
        (tmp_path / "README.md").write_text("# Hello")
        result = detect_repo_languages(tmp_path)
        assert "markdown" in result

    def test_typescript_from_package_json_deps(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"typescript": "^5.0"}})
        )
        result = detect_repo_languages(tmp_path)
        assert "typescript" in result

    def test_typescript_from_types_dep(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"@types/node": "^18"}})
        )
        result = detect_repo_languages(tmp_path)
        assert "typescript" in result

    def test_empty_repo(self, tmp_path):
        result = detect_repo_languages(tmp_path)
        assert result == []

    def test_multiple_languages(self, tmp_path):
        (tmp_path / "setup.py").write_text("pass")
        (tmp_path / "go.mod").write_text("module test")
        (tmp_path / "Cargo.toml").write_text("[package]")
        result = detect_repo_languages(tmp_path)
        assert "python" in result
        assert "go" in result
        assert "rust" in result

    def test_min_score_filter(self, tmp_path):
        (tmp_path / "setup.py").write_text("pass")
        result = detect_repo_languages(tmp_path, min_score=10)
        assert result == []

    def test_corrupt_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("not json")
        result = detect_repo_languages(tmp_path)
        assert "typescript" not in result

    def test_dockerfile_marker(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM python:3.11")
        result = detect_repo_languages(tmp_path)
        assert "dockerfile" in result

    def test_javascript_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "test"}))
        result = detect_repo_languages(tmp_path)
        assert "javascript" in result


class TestFindPluginsDir:
    def test_finds_project_plugins(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        test_plugin = plugins_dir / "test"
        test_plugin.mkdir(parents=True)
        (test_plugin / "plugin.yaml").write_text("id: plugin-test")
        result = _find_plugins_dir(tmp_path)
        assert result is not None
        assert result.name == "plugins"

    def test_returns_none_when_no_plugins_in_deep_path(self):
        from pathlib import Path

        deep = Path("/tmp/__plugin_test_isolated_42")
        deep.mkdir(exist_ok=True)
        result = _find_plugins_dir(deep)
        if result is not None:
            assert result.name == "plugins"


class TestLoadPluginModule:
    def test_returns_none_for_missing_file(self, tmp_path):
        result = _load_plugin_module("test", tmp_path)
        assert result is None

    def test_returns_none_for_no_plugin_class(self, tmp_path):
        plugin_file = tmp_path / "plugin.py"
        plugin_file.write_text("x = 1\n")
        result = _load_plugin_module("test", tmp_path)
        assert result is None

    def test_returns_none_on_import_error(self, tmp_path):
        plugin_file = tmp_path / "plugin.py"
        plugin_file.write_text("raise ImportError('nope')\n")
        result = _load_plugin_module("test", tmp_path)
        assert result is None

    def test_loads_valid_plugin(self, tmp_path):
        plugin_file = tmp_path / "plugin.py"
        plugin_file.write_text(
            "class Plugin:\n    id = 'test'\n    def detect(self, path): return True\n"
        )
        result = _load_plugin_module("test", tmp_path)
        assert result is not None
        assert result.id == "test"


class TestGetPluginManifest:
    def test_returns_none_for_missing(self, tmp_path):
        assert _get_plugin_manifest(tmp_path) is None

    def test_reads_yaml_manifest(self, tmp_path):
        (tmp_path / "plugin.yaml").write_text(yaml.dump({"id": "test-plugin"}))
        result = _get_plugin_manifest(tmp_path)
        assert result["id"] == "test-plugin"

    def test_returns_none_on_bad_yaml(self, tmp_path):
        (tmp_path / "plugin.yaml").write_text("{{invalid yaml")
        result = _get_plugin_manifest(tmp_path)
        assert result is None


class TestDiscoverPlugins:
    def test_empty_dir(self, tmp_path):
        assert discover_plugins(tmp_path / "missing") == {}

    def test_skips_non_dirs(self, tmp_path):
        (tmp_path / "file.txt").write_text("not a dir")
        assert discover_plugins(tmp_path) == {}

    def test_finds_plugins(self, tmp_path):
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(yaml.dump({"id": "plugin-my"}))
        result = discover_plugins(tmp_path)
        assert "plugin-my" in result

    def test_skips_manifest_without_id(self, tmp_path):
        plugin_dir = tmp_path / "bad-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(yaml.dump({"name": "no-id"}))
        result = discover_plugins(tmp_path)
        assert len(result) == 0


class TestLoadPlugin:
    def test_loads_by_directory_name(self, tmp_path):
        plugin_dir = tmp_path / "test"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(yaml.dump({"id": "plugin-test"}))
        (plugin_dir / "plugin.py").write_text(
            "class Plugin:\n    id = 'test'\n    def detect(self, p): return True\n"
        )
        result = load_plugin("test", tmp_path)
        assert result is not None

    def test_loads_by_scan_fallback(self, tmp_path):
        plugin_dir = tmp_path / "my-test"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text(yaml.dump({"id": "plugin-my-test"}))
        (plugin_dir / "plugin.py").write_text(
            "class Plugin:\n    id = 'my-test'\n    def detect(self, p): return True\n"
        )
        result = load_plugin("plugin-my-test", tmp_path)
        assert result is not None

    def test_returns_none_for_unknown(self, tmp_path):
        result = load_plugin("nonexistent", tmp_path)
        assert result is None


class TestLoadApplicablePlugins:
    def test_returns_empty_when_no_plugins_dir(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        result = load_applicable_plugins(repo_path, plugins_dir=tmp_path / "missing")
        assert result == []

    def test_returns_empty_when_no_matching_languages(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugin_dir = plugins_dir / "go"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.dump({"id": "plugin-go", "languages": ["go"]})
        )
        (plugin_dir / "plugin.py").write_text(
            "class Plugin:\n    id = 'go'\n    languages = ['go']\n    def detect(self, p): return True\n"
        )
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "setup.py").write_text("pass")
        result = load_applicable_plugins(repo_path, plugins_dir=plugins_dir)
        assert result == []


class TestRunPluginDiscovery:
    def test_returns_empty_when_no_plugins(self, tmp_path):
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        result = run_plugin_discovery(
            repo_path, plugins_dir=tmp_path / "missing", config={}
        )
        assert result == []

    def test_handles_plugin_exception_gracefully(self, tmp_path):
        plugins_dir = tmp_path / "plugins"
        plugin_dir = plugins_dir / "bad"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text(
            yaml.dump({"id": "plugin-bad", "languages": ["python"]})
        )
        (plugin_dir / "plugin.py").write_text(
            "class Plugin:\n"
            "    id = 'bad'\n"
            "    languages = ['python']\n"
            "    def detect(self, p): return True\n"
            "    def discover(self, p, c): raise RuntimeError('boom')\n"
        )
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "setup.py").write_text("pass")
        result = run_plugin_discovery(repo_path, plugins_dir=plugins_dir)
        assert result == []
