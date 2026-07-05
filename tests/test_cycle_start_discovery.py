"""Tests for cycle-start BackendModelDiscovery wiring (Phase 2, T2.4).

Covers:
  * ``model_tiers.yaml`` present → ``load_model_tiers`` returns the config and
    a ``BackendModelDiscovery`` is constructed with the right tier_config (the
    exact 4-line construction pattern wired at cli.py cycle-start).
  * File absent → ``load_model_tiers`` returns ``{}`` → discovery is ``None``
    (C2 identity — ctx.discovery stays None, template unchanged).
  * Backend with no tier entry for the active backend → empty tier_config.

These tests exercise the building blocks cli.py uses at cycle-start
(``load_model_tiers`` + ``BackendModelDiscovery``). The construction pattern
mirrors cli.py:``_discovery = BackendModelDiscovery(...) if _tier_config else None``.
"""

from __future__ import annotations

from pathlib import Path

from bluei.engine.model_discovery import (
    BackendModelDiscovery,
    ModelTier,
    load_model_tiers,
)


def _construct_discovery(state_dir: Path, backend: str = "claude"):
    """Replicate cli.py's cycle-start construction (T2.4 wiring)."""
    _tiers_path = state_dir / "model_tiers.yaml"
    _tier_config = load_model_tiers(_tiers_path)
    return (
        BackendModelDiscovery(
            tier_config=_tier_config.get(backend, {}), backend=backend
        )
        if _tier_config
        else None
    )


class TestCycleStartDiscovery:
    def test_yaml_present_constructs_discovery(self, tmp_path):
        (tmp_path / "model_tiers.yaml").write_text(
            "claude:\n  tier-0: claude-3-5-haiku\n  tier-2: claude-sonnet-4\n"
        )
        discovery = _construct_discovery(tmp_path)
        assert discovery is not None
        assert isinstance(discovery, BackendModelDiscovery)
        assert discovery.backend == "claude"
        assert discovery.tier_config[ModelTier.TIER_0] == "claude-3-5-haiku"
        assert discovery.tier_config[ModelTier.TIER_2] == "claude-sonnet-4"

    def test_yaml_absent_discovery_is_none(self, tmp_path):
        # C2 inert default: no model_tiers.yaml → discovery None → identity.
        discovery = _construct_discovery(tmp_path)
        assert discovery is None

    def test_yaml_empty_file_discovery_is_none(self, tmp_path):
        (tmp_path / "model_tiers.yaml").write_text("")
        discovery = _construct_discovery(tmp_path)
        assert discovery is None

    def test_wrong_backend_yields_empty_tier_config(self, tmp_path):
        # yaml only has opencode tiers; active backend is claude → empty config.
        (tmp_path / "model_tiers.yaml").write_text("opencode:\n  tier-0: gpt-4o-mini\n")
        discovery = _construct_discovery(tmp_path, backend="claude")
        assert discovery is not None  # _tier_config is non-empty (opencode)
        assert discovery.tier_config == {}  # but claude has no entries
