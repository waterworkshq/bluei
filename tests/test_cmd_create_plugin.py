#!/usr/bin/env python3
"""Tests for the `bluei create-plugin` scaffolding command."""

from __future__ import annotations

import ast
import io
import contextlib
from pathlib import Path

import pytest
import yaml

from bin.cmd_create_plugin import (
    _classify_id,
    _cmd_create_plugin,
    _derive_plugin_id,
    _plugin_id_to_class,
    _plugin_id_to_display,
    _resolve_paths,
    render_health_mapping_yaml,
    render_plugin_py,
    render_plugin_yaml,
    render_test_py,
)
from bin.help_text import HELP_TEXT


# ── Pure-function tests (no I/O) ────────────────────────────────


class TestIdDerivation:
    def test_classify_lowercases_and_dedashes(self):
        assert _classify_id("MyTool") == "mytool"
        assert _classify_id("My-Tool") == "my-tool"
        assert _classify_id("my tool") == "my-tool"
        assert _classify_id("my--tool") == "my-tool"
        assert _classify_id("--my-tool--") == "my-tool"

    def test_derive_plugin_id_uses_prefix(self):
        assert _derive_plugin_id("ruff") == "plugin-ruff"
        assert _derive_plugin_id("ESLint") == "plugin-eslint"
        assert _derive_plugin_id("static check") == "plugin-static-check"

    def test_class_name_for_simple_id(self):
        assert _plugin_id_to_class("plugin-ruff") == "RuffPlugin"
        assert _plugin_id_to_class("plugin-eslint") == "EslintPlugin"
        assert _plugin_id_to_class("plugin-my-tool") == "MyToolPlugin"
        assert _plugin_id_to_class("plugin-staticcheck") == "StaticcheckPlugin"

    def test_class_name_fallback(self):
        assert _plugin_id_to_class("bare") == "BarePlugin"
        assert _plugin_id_to_class("plugin-") == "Plugin"

    def test_display_name_for_simple_id(self):
        assert _plugin_id_to_display("plugin-ruff") == "Ruff Plugin"
        assert _plugin_id_to_display("plugin-my-tool") == "My Tool Plugin"
        assert _plugin_id_to_display("plugin-") == "Plugin"


class TestRenderers:
    def test_plugin_yaml_is_valid_yaml(self):
        text = render_plugin_yaml(
            plugin_id="plugin-ruff",
            display_name="Ruff Plugin",
            language="python",
            tool="ruff",
            author="bluei",
        )
        parsed = yaml.safe_load(text)
        assert parsed["id"] == "plugin-ruff"
        assert parsed["name"] == "Ruff Plugin"
        assert parsed["version"] == "0.1.0"
        assert parsed["author"] == "bluei"
        assert parsed["languages"] == ["python"]
        assert parsed["requires_container"] is False
        assert parsed["discovery"]["tool"] == "ruff"
        assert parsed["discovery"]["output_format"] == "json"
        assert isinstance(parsed["discovery"]["rules"], list)

    def test_plugin_py_compiles_and_has_expected_symbols(self):
        text = render_plugin_py(
            plugin_id="plugin-ruff",
            display_name="Ruff Plugin",
            language="python",
            tool="ruff",
            class_name="RuffPlugin",
        )
        tree = ast.parse(text)
        # Find the class definition
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        assert len(classes) == 1
        cls = classes[0]
        assert cls.name == "Plugin"
        # Required abstract methods
        method_names = {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}
        assert "discover" in method_names or "parse_output" in method_names
        # Properties
        prop_names = {
            m.name
            for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert "id" in prop_names
        assert "name" in prop_names
        assert "languages" in prop_names
        assert "rules" in prop_names

    def test_health_mapping_is_valid_yaml(self):
        text = render_health_mapping_yaml(plugin_id="plugin-ruff")
        parsed = yaml.safe_load(text)
        assert "rule_to_component" in parsed
        assert "category_inference" in parsed
        assert "ruff" in parsed["category_inference"]

    def test_test_file_is_valid_python(self):
        text = render_test_py(plugin_id="plugin-ruff", class_name="RuffPlugin")
        ast.parse(text)
        assert "def test_id_matches_manifest" in text
        assert "def test_languages_is_nonempty_list" in text
        assert "def test_discover_returns_list" in text

    def test_test_file_uses_underscore_module_name(self):
        """Test file path must be importable — no dashes in module name."""
        from bin.cmd_create_plugin import _resolve_paths

        with __import__("tempfile").TemporaryDirectory() as tmp:
            plugin_dir, test_file = _resolve_paths(
                plugins_dir=Path(tmp) / "plugins",
                plugin_id="plugin-my-tool",
            )
            assert test_file.name == "test_plugin_my_tool.py"
            assert plugin_dir.name == "plugin-my-tool"


# ── Integration tests (filesystem) ──────────────────────────────


def _capture(coro_args):
    """Run a CLI invocation, capturing stdout and stderr separately."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        # _cmd_create_plugin takes a list of args directly
        code = _cmd_create_plugin(coro_args)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def sandbox(tmp_path):
    """Provide a fresh tmp directory with a plugins/ subdir."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    return tmp_path, plugins_dir


class TestCreatePluginEndToEnd:
    def test_creates_expected_files(self, sandbox):
        _, plugins_dir = sandbox
        code, out, err = _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 0, f"unexpected stderr: {err}"
        plugin_dir = plugins_dir / "plugin-ruff"
        assert plugin_dir.is_dir()
        assert (plugin_dir / "plugin.yaml").is_file()
        assert (plugin_dir / "plugin.py").is_file()
        assert (plugin_dir / "health_mapping.yaml").is_file()
        # Test file written under the parent directory's tests/ folder
        project_root, _ = sandbox
        assert (project_root / "tests" / "test_plugin_ruff.py").is_file()
        # Stdout mentions what was created
        assert "Created plugin 'plugin-ruff'" in out
        assert "ruff" in out

    def test_plugin_id_derived_from_tool(self, sandbox):
        _, plugins_dir = sandbox
        code, _, err = _capture(
            [
                "--language",
                "go",
                "--tool",
                "staticcheck",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 0, err
        assert (plugins_dir / "plugin-staticcheck").is_dir()

    def test_plugin_id_override(self, sandbox):
        _, plugins_dir = sandbox
        code, out, err = _capture(
            [
                "--language",
                "go",
                "--tool",
                "staticcheck",
                "--plugin-id",
                "go-static-linter",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 0, err
        assert (plugins_dir / "plugin-go-static-linter").is_dir()
        assert "plugin-go-static-linter" in out
        # Generated manifest uses the override id
        manifest = yaml.safe_load(
            (plugins_dir / "plugin-go-static-linter" / "plugin.yaml").read_text()
        )
        assert manifest["id"] == "plugin-go-static-linter"
        assert manifest["discovery"]["tool"] == "staticcheck"

    def test_plugin_id_override_already_prefixed(self, sandbox):
        """If user passes --plugin-id plugin-foo we don't double-prefix."""
        _, plugins_dir = sandbox
        code, _, err = _capture(
            [
                "--language",
                "ruby",
                "--tool",
                "rubocop",
                "--plugin-id",
                "plugin-rubocop-pro",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 0, err
        assert (plugins_dir / "plugin-rubocop-pro").is_dir()
        assert not (plugins_dir / "plugin-plugin-rubocop-pro").exists()

    def test_generated_plugin_yaml_has_expected_structure(self, sandbox):
        _, plugins_dir = sandbox
        _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        manifest_path = plugins_dir / "plugin-ruff" / "plugin.yaml"
        manifest = yaml.safe_load(manifest_path.read_text())
        # Required top-level keys
        for key in ("id", "name", "version", "author", "languages", "discovery"):
            assert key in manifest, f"missing key {key} in {manifest_path}"
        # Discovery section shape
        disc = manifest["discovery"]
        assert disc["tool"] == "ruff"
        assert isinstance(disc["tool_args"], list)
        assert disc["output_format"] == "json"
        assert isinstance(disc["rules"], list)

    def test_generated_plugin_py_is_syntactically_valid(self, sandbox):
        _, plugins_dir = sandbox
        _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        plugin_py = (plugins_dir / "plugin-ruff" / "plugin.py").read_text()
        # Should compile without error
        ast.parse(plugin_py)

    def test_generated_plugin_loads_via_plugin_loader(self, sandbox):
        """Loading the scaffold via PluginLoader produces a usable plugin.

        Snapshot/restore the global HealthEngine class-level mappings so
        registering our scaffold's health_mapping.yaml does not pollute
        other tests in the suite.
        """
        from bluei.app.health import HealthEngine
        from bluei.app.plugins import PluginLoader

        _, plugins_dir = sandbox
        _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        rule_snapshot = dict(HealthEngine.RULE_TO_COMPONENT)
        cat_snapshot = dict(HealthEngine.CATEGORY_INFERENCE)
        try:
            loader = PluginLoader(plugins_dir)
            loader.discover()
            plugin = loader.load("plugin-ruff")
            assert plugin is not None
            assert plugin.id == "plugin-ruff"
            assert plugin.languages == ["python"]
            assert isinstance(plugin.rules, list)
        finally:
            HealthEngine.RULE_TO_COMPONENT.clear()
            HealthEngine.RULE_TO_COMPONENT.update(rule_snapshot)
            HealthEngine.CATEGORY_INFERENCE.clear()
            HealthEngine.CATEGORY_INFERENCE.update(cat_snapshot)

    def test_generated_test_file_is_syntactically_valid(self, sandbox):
        _, plugins_dir = sandbox
        _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        project_root, _ = sandbox
        test_path = project_root / "tests" / "test_plugin_ruff.py"
        test_src = test_path.read_text()
        ast.parse(test_src)
        # Module name is importable (no dashes)
        assert "test_plugin_ruff.py" in test_path.name

    def test_refuses_overwrite_without_force(self, sandbox):
        _, plugins_dir = sandbox
        args = [
            "--language",
            "python",
            "--tool",
            "ruff",
            "--plugins-dir",
            str(plugins_dir),
        ]
        first_code, _, _ = _capture(args)
        assert first_code == 0
        second_code, _, err = _capture(args)
        assert second_code == 1
        assert "already exists" in err
        assert "plugin-ruff" in err

    def test_force_flag_overwrites(self, sandbox):
        _, plugins_dir = sandbox
        args = [
            "--language",
            "python",
            "--tool",
            "ruff",
            "--plugins-dir",
            str(plugins_dir),
        ]
        _capture(args)
        # Mutate one of the generated files to confirm overwrite
        (plugins_dir / "plugin-ruff" / "plugin.yaml").write_text("tampered: true\n")
        code, _, err = _capture(args + ["--force"])
        assert code == 0, err
        manifest = yaml.safe_load(
            (plugins_dir / "plugin-ruff" / "plugin.yaml").read_text()
        )
        assert manifest["id"] == "plugin-ruff"
        assert "tampered" not in manifest

    def test_missing_language_fails(self, sandbox):
        _, plugins_dir = sandbox
        code, _, err = _capture(
            [
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 1
        assert "--language" in err

    def test_missing_tool_fails(self, sandbox):
        _, plugins_dir = sandbox
        code, _, err = _capture(
            [
                "--language",
                "python",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 1
        assert "--tool" in err

    def test_help_prints_usage(self, sandbox):
        code, out, _ = _capture(["--help"])
        assert code == 0
        assert "create-plugin" in out
        assert "--language" in out
        assert "--tool" in out

    def test_help_text_entry_registered(self):
        assert "create-plugin" in HELP_TEXT
        text = HELP_TEXT["create-plugin"]
        assert "create-plugin" in text
        assert "--language" in text
        assert "--tool" in text

    def test_description_is_printed_but_not_embedded(self, sandbox):
        _, plugins_dir = sandbox
        code, out, _ = _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--description",
                "wraps ruff for super-fast linting",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 0
        assert "wraps ruff for super-fast linting" in out
        # Description is informational only — not embedded in manifest
        manifest = yaml.safe_load(
            (plugins_dir / "plugin-ruff" / "plugin.yaml").read_text()
        )
        assert "description" not in manifest

    def test_custom_author_is_embedded(self, sandbox):
        _, plugins_dir = sandbox
        code, _, _ = _capture(
            [
                "--language",
                "python",
                "--tool",
                "ruff",
                "--author",
                "acme-corp",
                "--plugins-dir",
                str(plugins_dir),
            ]
        )
        assert code == 0
        manifest = yaml.safe_load(
            (plugins_dir / "plugin-ruff" / "plugin.yaml").read_text()
        )
        assert manifest["author"] == "acme-corp"


# ── CLI dispatch (top-level main) ───────────────────────────────


class TestBlueiDispatch:
    def test_bluei_dispatches_create_plugin(self, sandbox, monkeypatch, capsys):
        import sys
        from bin import bluei

        _, plugins_dir = sandbox
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "bluei",
                "create-plugin",
                "--language",
                "python",
                "--tool",
                "ruff",
                "--plugins-dir",
                str(plugins_dir),
            ],
        )
        code = bluei.main()
        out = capsys.readouterr().out
        assert code == 0
        assert (plugins_dir / "plugin-ruff").is_dir()
        assert "Created plugin" in out

    def test_unknown_command_still_works(self, monkeypatch, capsys):
        """Regression: adding create-plugin must not break the unknown-cmd path."""
        import sys
        from bin import bluei

        monkeypatch.setattr(sys, "argv", ["bluei", "this-is-fake"])
        code = bluei.main()
        assert code == 1
        err = capsys.readouterr().err
        assert "not a command" in err
