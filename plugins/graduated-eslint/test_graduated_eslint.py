#!/usr/bin/env python3
"""Test scaffold for the auto-generated pack ``{pack_id}``.

Proves the pack loads via ``PluginLoader`` and exposes its graduated rule.
Extend the planted-violation assertions during curation (the generator cannot
guess a real fixture line for every rule shape).
"""

from pathlib import Path

from bluei.app.plugins import PluginLoader


PLUGINS_DIR = Path(__file__).resolve().parents[1]


def test_pack_loads():
    loader = PluginLoader(PLUGINS_DIR)
    plugin = loader.load('graduated-eslint')
    assert plugin is not None
    assert plugin.id == 'graduated-eslint'
    assert 'javascript' in plugin.languages

    catalog = loader.detector_catalog()
    rule_ids = [entry["rule"] for entry in catalog]
    assert plugin.rules[0] in rule_ids
