"""Tests for config schema migration and backward compatibility."""

import pytest
from pathlib import Path

from bluei.app.config import ConfigManager, _deep_merge
from bluei.app.models import RepoConfig, ONBOARDING_VERSION


# ---------------------------------------------------------------------------
# V1 backward compatibility — old configs load without error
# ---------------------------------------------------------------------------

def test_v1_config_loads_safely(tmp_path):
    """A minimal V1-style config (no meta, no safety, no limits) loads."""
    v1_config = tmp_path / "repos" / "demo" / "config.yaml"
    v1_config.parent.mkdir(parents=True)
    v1_config.write_text(
        "id: repo-demo\n"
        "name: demo\n"
        "path: /tmp/demo\n"
        "language: python\n"
        "plugin_id: plugin-python\n"
        "enabled: true\n"
        "discovery:\n"
        "  ecosystem: python\n"
    )
    cm = ConfigManager(tmp_path)
    config = cm.load_repo_config("demo")
    assert config is not None
    assert config.name == "demo"
    assert config.language == "python"


def test_v1_config_gets_migration_defaults(tmp_path):
    """V1 configs receive auto-inferred safety, meta, and review_care."""
    v1_config = tmp_path / "repos" / "demo" / "config.yaml"
    v1_config.parent.mkdir(parents=True)
    v1_config.write_text(
        "id: repo-demo\n"
        "name: demo\n"
        "path: /tmp/demo\n"
        "language: python\n"
        "plugin_id: plugin-python\n"
        "enabled: true\n"
    )
    cm = ConfigManager(tmp_path)
    config = cm.load_repo_config("demo")

    # from_dict assigns onboarding_version 1 for legacy configs
    assert config.meta["onboarding_version"] == 1

    # Safety defaults to observe + conservative
    assert config.safety["mode"] == "observe"
    assert config.safety["profile"] == "conservative"

    # Review care is populated
    assert config.review_care["enabled"] is True


# ---------------------------------------------------------------------------
# Current version
# ---------------------------------------------------------------------------

def test_onboarding_version_is_2():
    """The current onboarding version constant."""
    assert ONBOARDING_VERSION == 2


def test_v2_config_has_meta(tmp_path):
    """render_config_from_template sets meta.onboarding_version."""
    cm = ConfigManager(tmp_path)
    config = cm.render_config_from_template(
        name="demo",
        path="/tmp/demo",
        language="python",
        # No template_name — kwargs-only config
    )
    assert config.meta.get("onboarding_version") == ONBOARDING_VERSION


def test_render_config_from_template_sets_inferred_by(tmp_path):
    """Without a template, inferred_by defaults to 'template'."""
    cm = ConfigManager(tmp_path)
    config = cm.render_config_from_template(
        name="demo",
        path="/tmp/demo",
        language="python",
    )
    assert config.meta["inferred_by"] == "template"


def test_render_config_from_template_with_real_template(tmp_path):
    """When the template file exists in the workspace, it is deep-merged."""
    cm = ConfigManager(tmp_path)

    # Seed a template into the temp workspace
    tpl_dir = tmp_path / "templates" / "repos"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    (tpl_dir / "python-library.yaml").write_text(
        "plugin_id: python\n"
        "fix_engine: deterministic\n"
        "limits:\n"
        "  max_files_changed: 3\n"
        "  max_loc_diff: 120\n"
        "safety:\n"
        "  mode: observe\n"
        "  profile: conservative\n"
    )

    config = cm.render_config_from_template(
        name="demo",
        path="/tmp/demo",
        language="python",
        template_name="python-library",
        limits={"max_files_changed": 10},
    )

    # Deep-merged: override wins for max_files_changed, template default
    # survives for max_loc_diff
    assert config.limits["max_files_changed"] == 10
    assert config.limits["max_loc_diff"] == 120


# ---------------------------------------------------------------------------
# Deep merge unit test
# ---------------------------------------------------------------------------

def test_deep_merge_unit():
    """_deep_merge preserves sub-keys from base when override only touches some."""
    base = {"limits": {"a": 1, "b": 2}, "safety": {"mode": "observe"}}
    override = {"limits": {"a": 99}}
    result = _deep_merge(base, override)
    assert result["limits"]["a"] == 99
    assert result["limits"]["b"] == 2
    assert result["safety"]["mode"] == "observe"


# ---------------------------------------------------------------------------
# from_dict backward compat
# ---------------------------------------------------------------------------

def test_from_dict_infers_safety_from_github(tmp_path):
    """If github.live_actions is true, safety mode upgrades to merge."""
    config = RepoConfig.from_dict({
        "id": "repo-x",
        "name": "x",
        "path": "/tmp/x",
        "language": "python",
        "github": {"live_actions": True, "auto_merge": False},
    })
    assert config.safety["mode"] == "merge"
    assert config.safety["profile"] == "balanced"


def test_from_dict_minimal_fields():
    """Bare minimum fields produce a valid RepoConfig."""
    config = RepoConfig.from_dict({
        "id": "repo-min",
        "name": "min",
        "path": "/tmp/min",
        "language": "go",
    })
    assert config.framework is None
    assert config.safety["mode"] == "observe"
    assert config.review_care["enabled"] is True
    errors = config.validate()
    assert errors == []
