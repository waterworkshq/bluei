#!/usr/bin/env python3
"""Pytest-native tests for Plugin System."""

from pathlib import Path
import shutil

import pytest

from qa_agent.plugins import PluginLoader


@pytest.fixture
def plugins_dir():
    return Path(__file__).resolve().parents[1] / 'plugins'


@pytest.fixture
def loader(plugins_dir):
    return PluginLoader(plugins_dir)


@pytest.fixture
def temp_repo(tmp_path):
    return tmp_path / 'repo'


def test_discover_plugins(loader):
    manifests = loader.discover()
    plugin_ids = [m.get('id') for m in manifests]
    assert 'plugin-test' in plugin_ids


def test_load_plugin(loader):
    plugin = loader.load('plugin-test')
    assert plugin is not None
    assert plugin.id == 'plugin-test'
    assert plugin.name == 'Test Plugin'
    assert 'test' in plugin.languages


def test_get_plugin(loader):
    loader.load('plugin-test')
    plugin = loader.get('plugin-test')
    assert plugin is not None
    assert plugin.id == 'plugin-test'


def test_get_for_language(loader):
    plugin = loader.get_for_language('test')
    assert plugin is not None
    assert plugin.id == 'plugin-test'


def test_list_loaded(loader):
    loader.load('plugin-test')
    loaded = loader.list_loaded()
    assert 'plugin-test' in loaded


def test_get_manifest(loader):
    loader.discover()
    manifest = loader.get_manifest('plugin-test')
    assert manifest is not None
    assert manifest['id'] == 'plugin-test'
    assert manifest['name'] == 'Test Plugin'


def test_plugin_discover_method(loader, temp_repo):
    temp_repo.mkdir()
    (temp_repo / 'test.txt').write_text('test content')
    plugin = loader.load('plugin-test')
    findings = plugin.discover(temp_repo, {})
    assert len(findings) > 0
    assert findings[0].rule == 'test-rule'


def test_plugin_detect_method(loader, tmp_path):
    matching_repo = tmp_path / 'matching-repo'
    matching_repo.mkdir()
    (matching_repo / 'test.txt').write_text('test content')

    other_repo = tmp_path / 'other-repo'
    other_repo.mkdir()

    plugin = loader.load('plugin-test')
    assert plugin.detect(matching_repo) is True
    assert plugin.detect(other_repo) is False
