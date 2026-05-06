#!/usr/bin/env python3
"""Pytest-native tests for Python and TypeScript plugins."""

from pathlib import Path

import pytest

from qa_agent.plugins import PluginLoader


@pytest.fixture
def plugins_dir():
    return Path(__file__).resolve().parents[1] / 'plugins'


@pytest.fixture
def loader(plugins_dir):
    return PluginLoader(plugins_dir)


def test_detect_python_repo(loader, tmp_path):
    (tmp_path / 'setup.py').write_text('# setup')
    plugin = loader.load('plugin-python')
    assert plugin.detect(tmp_path) is True


def test_detect_non_python_repo(loader, tmp_path):
    plugin = loader.load('plugin-python')
    assert plugin.detect(tmp_path) is False


def test_discover_broad_except(loader, tmp_path):
    (tmp_path / 'setup.py').write_text('# setup')
    (tmp_path / 'test.py').write_text('''\ntry:\n    do_something()\nexcept:\n    pass\n''')
    plugin = loader.load('plugin-python')
    findings = plugin.discover(tmp_path, {})
    broad_excepts = [f for f in findings if f.rule == 'broad-except']
    assert len(broad_excepts) > 0


def test_discover_trailing_whitespace(loader, tmp_path):
    (tmp_path / 'setup.py').write_text('# setup')
    (tmp_path / 'test.py').write_text('x = 1   \ny = 2\n')
    plugin = loader.load('plugin-python')
    findings = plugin.discover(tmp_path, {})
    whitespace = [f for f in findings if f.rule == 'trailing-whitespace']
    assert len(whitespace) > 0


def test_detect_typescript_repo(loader, tmp_path):
    (tmp_path / 'tsconfig.json').write_text('{}')
    plugin = loader.load('plugin-typescript')
    assert plugin.detect(tmp_path) is True


def test_detect_javascript_repo(loader, tmp_path):
    (tmp_path / 'package.json').write_text('{"dependencies": {"express": "^4.0.0"}}')
    plugin = loader.load('plugin-typescript')
    assert plugin.detect(tmp_path) is True


def test_detect_non_js_repo(loader, tmp_path):
    plugin = loader.load('plugin-typescript')
    assert plugin.detect(tmp_path) is False


def test_discover_explicit_any(loader, tmp_path):
    (tmp_path / 'tsconfig.json').write_text('{}')
    (tmp_path / 'test.ts').write_text('const x: any = 1;\n')
    plugin = loader.load('plugin-typescript')
    findings = plugin.discover(tmp_path, {})
    any_findings = [f for f in findings if f.rule == 'type-explicit-any']
    assert len(any_findings) > 0


def test_detect_go_repo(loader, tmp_path):
    (tmp_path / 'go.mod').write_text('module test\n\ngo 1.21')
    plugin = loader.load('plugin-go')
    assert plugin.detect(tmp_path) is True


def test_detect_rust_repo(loader, tmp_path):
    (tmp_path / 'Cargo.toml').write_text('[package]\nname = "test"')
    plugin = loader.load('plugin-rust')
    assert plugin.detect(tmp_path) is True
