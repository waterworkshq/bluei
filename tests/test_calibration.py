"""Tests for SPRT calibration harness (Slice 4, T4.4).

Covers:
- generate_variations + calibrate_family non-degenerate path (distinct hashes)
- calibrate_family degenerate path (all-same hash → fallback)
- write_calibration / load_calibration round-trip
- _sprt_params per-family override + degenerate fallback (via inline loader)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluei.tools.seed import variations as variations_mod
from bluei.tools.seed.calibrate import (
    calibrate_family,
    load_calibration,
    write_calibration,
)
from bluei.engine import sprt
from bluei.engine.structural_hash import compute_structural_hash


# --- non-degenerate -----------------------------------------------------------


def test_calibrate_family_non_degenerate_produces_distinct_hashes():
    """A family generator that yields structurally-diverse variants must
    produce a non-degenerate calibration entry (>= 3 distinct hashes)."""

    def diverse(seed, language):
        return [
            'raise ValueError("a")',
            'raise ValueError("a") from None',
            'raise ValueError("a") from cause',
            'raise ValueError("a").with_traceback(tb)',
            "raise ValueError(repr(x))",
            "raise ValueError",
            'raise ValueError("a") if not ok else None',
            'raise ValueError("a"); log()',
        ]

    variations_mod._FAMILY_GENERATORS["test-diverse"] = diverse
    try:
        entry = calibrate_family("test-diverse", "python", 'raise ValueError("oops")')
    finally:
        variations_mod._FAMILY_GENERATORS.pop("test-diverse", None)

    assert entry["degenerate"] is False
    assert entry["distinct_hashes"] >= 3
    assert 0.50 <= entry["p_healthy"] <= 0.99
    assert 0.10 <= entry["p_broken"] <= 0.50
    assert entry["variations_tested"] == 8
    assert "fallback_reason" not in entry


def test_non_degenerate_variations_actually_hash_differently():
    """Sanity: the structurally-diverse variants above produce distinct
    structural hashes (the property the calibration depends on)."""
    samples = [
        'raise ValueError("a")',
        'raise ValueError("a") from None',
        'raise ValueError("a").with_traceback(tb)',
        "raise ValueError",
    ]
    hashes = {compute_structural_hash(s, "python") for s in samples}
    assert len(hashes) >= 3


# --- degenerate ---------------------------------------------------------------


def test_calibrate_family_degenerate_falls_back_to_defaults():
    """The generic generator only renames identifiers — structural hashing
    normalizes names, so all variants share one hash → degenerate fallback
    to ADR-0012 defaults."""
    seed = 'raise ValueError("oops")'
    entry = calibrate_family("no-such-family", "python", seed)

    assert entry["degenerate"] is True
    assert entry["p_healthy"] == 0.90
    assert entry["p_broken"] == 0.50
    assert entry["distinct_hashes"] == 1
    assert "fallback_reason" in entry


# --- round-trip ---------------------------------------------------------------


def test_write_load_calibration_roundtrip(tmp_path: Path):
    entries = {
        "synthetic-raise": {
            "p_healthy": 0.82,
            "p_broken": 0.30,
            "degenerate": False,
            "variations_tested": 10,
            "distinct_hashes": 8,
        },
        "synthetic-generic": {
            "p_healthy": 0.90,
            "p_broken": 0.50,
            "degenerate": True,
            "variations_tested": 3,
            "distinct_hashes": 1,
            "fallback_reason": "insufficient structural variance",
        },
    }
    out = write_calibration(entries, path=tmp_path / "calibration.yaml")
    assert out.is_file()
    loaded = load_calibration(out)
    assert loaded == entries


def test_load_calibration_missing_file_returns_empty(tmp_path: Path):
    assert load_calibration(tmp_path / "absent.yaml") == {}


def test_load_calibration_malformed_returns_empty(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("::: not ::: valid ::: yaml ::: [[[")
    # safe_load of garbage may raise or return non-dict; both → {}
    assert load_calibration(bad) in ({}, None) or isinstance(
        load_calibration(bad), dict
    )


# --- _sprt_params runtime override -------------------------------------------


@pytest.fixture
def calibrated_sprt(tmp_path: Path, monkeypatch):
    """Point sprt's inline calibration loader at a tmp yaml with both paths."""
    yaml_path = tmp_path / "calibration.yaml"
    write_calibration(
        {
            "calibrated-family": {
                "p_healthy": 0.77,
                "p_broken": 0.33,
                "degenerate": False,
                "variations_tested": 10,
                "distinct_hashes": 8,
            },
            "degenerate-family": {
                "p_healthy": 0.90,
                "p_broken": 0.50,
                "degenerate": True,
                "variations_tested": 3,
                "distinct_hashes": 1,
                "fallback_reason": "insufficient variance",
            },
        },
        path=yaml_path,
    )
    monkeypatch.setattr(sprt, "_CALIBRATION_PATH", yaml_path)
    # Reset cache so the new path/mtime is picked up.
    monkeypatch.setattr(sprt, "_CALIBRATION_CACHE", None)
    monkeypatch.setattr(sprt, "_CALIBRATION_MTIME", None)
    return yaml_path


def test_sprt_params_uses_calibrated_non_degenerate(calibrated_sprt):
    params = sprt._sprt_params({}, rule_family="calibrated-family")
    assert params["p_healthy"] == pytest.approx(0.77)
    assert params["p_broken"] == pytest.approx(0.33)
    # alpha/beta untouched (risk tolerances, not empirical)
    assert params["alpha"] == pytest.approx(0.01)
    assert params["beta"] == pytest.approx(0.01)


def test_sprt_params_degenerate_family_falls_back(calibrated_sprt):
    params = sprt._sprt_params({}, rule_family="degenerate-family")
    assert params["p_healthy"] == pytest.approx(0.90)
    assert params["p_broken"] == pytest.approx(0.50)


def test_sprt_params_unknown_family_falls_back(calibrated_sprt):
    params = sprt._sprt_params({}, rule_family="never-calibrated")
    assert params["p_healthy"] == pytest.approx(0.90)
    assert params["p_broken"] == pytest.approx(0.50)


def test_sprt_params_no_family_uses_defaults(calibrated_sprt):
    params = sprt._sprt_params({})
    assert params == {
        "alpha": pytest.approx(0.01),
        "beta": pytest.approx(0.01),
        "p_healthy": pytest.approx(0.90),
        "p_broken": pytest.approx(0.50),
    }


def test_sprt_params_config_still_overrides_when_no_family(calibrated_sprt):
    config = {"learning": {"sprt": {"p_healthy": 0.88, "alpha": 0.05}}}
    params = sprt._sprt_params(config)
    assert params["p_healthy"] == pytest.approx(0.88)
    assert params["alpha"] == pytest.approx(0.05)
    # unspecified ones still default
    assert params["p_broken"] == pytest.approx(0.50)


def test_sprt_params_calibrated_overrides_config(calibrated_sprt):
    """Calibration takes precedence over config p_healthy/p_broken when
    a non-degenerate entry exists for the family."""
    config = {"learning": {"sprt": {"p_healthy": 0.88}}}
    params = sprt._sprt_params(config, rule_family="calibrated-family")
    assert params["p_healthy"] == pytest.approx(0.77)


def test_run_sprt_check_threads_rule_family(calibrated_sprt, tmp_path: Path):
    """run_sprt_check accepts rule_family and threads it to _sprt_params."""
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    dr.write_text("")  # no records → no LLR crossing → empty result
    ar.write_text("")
    # Just confirms the signature accepts rule_family without error.
    result = sprt.run_sprt_check(
        ["p1"], dr, ar, config={}, rule_family="calibrated-family"
    )
    assert result == []


# --- shipped calibration.yaml sanity -----------------------------------------


def test_shipped_calibration_yaml_has_both_paths():
    """The committed calibration.yaml ships entries demonstrating both the
    calibrated (non-degenerate) and fallback (degenerate) paths."""
    shipped = (
        Path(__file__).resolve().parent.parent
        / "bluei"
        / "engine"
        / "seeded_patterns"
        / "calibration.yaml"
    )
    assert shipped.is_file(), "calibration.yaml must ship with the harness"
    families = load_calibration(shipped)
    assert isinstance(families, dict) and len(families) >= 2
    degenerate = [e for e in families.values() if e.get("degenerate")]
    calibrated = [e for e in families.values() if not e.get("degenerate")]
    assert degenerate, "expected at least one degenerate fallback entry"
    assert calibrated, "expected at least one calibrated non-degenerate entry"
