"""Tests for the Golden Validation Bundle loader (Phase 1, alpha.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from bluei.engine.bundle_loader import (
    GoldenBundle,
    has_bundle_reference,
    load_bundles,
)


PRODUCT_FIXTURE_ID = "gb-ruff-b904-raise-with-from"


def _bundle_dict(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "id": "gb-test-bundle",
        "asset_class": "pattern",
        "asset_ref": None,
        "rule": "ruff-b904",
        "rule_family": "",
        "language": "python",
        "before": "raise ValueError('oops')",
        "after": "raise ValueError('oops') from None",
        "detector_before": "example.py:3:9: B904 ...",
        "detector_after": "",
        "validation_command": "ruff check --select B904",
        "source_finding_id": None,
        "extracted_at": "2026-06-27T00:00:00Z",
    }
    base.update(overrides)
    return base


def _write_bundle(path: Path, data: Dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# load_bundles — product fixtures only
# ---------------------------------------------------------------------------


def test_load_bundles_no_repo_state_dir_returns_product_fixture() -> None:
    bundles = load_bundles(repo_state_dir=None)
    ids = [b.id for b in bundles]
    assert PRODUCT_FIXTURE_ID in ids


def test_load_bundles_nonexistent_repo_state_dir_returns_product_only(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist"
    bundles = load_bundles(repo_state_dir=missing)
    ids = [b.id for b in bundles]
    assert PRODUCT_FIXTURE_ID in ids


# ---------------------------------------------------------------------------
# load_bundles — product + repo state
# ---------------------------------------------------------------------------


def test_load_bundles_with_repo_state_dir_merges_product_and_repo(
    tmp_path: Path,
) -> None:
    _write_bundle(
        tmp_path / "golden_bundles" / "repo-extra.yaml",
        _bundle_dict(id="gb-repo-extra", asset_ref="ref-extra"),
    )
    bundles = load_bundles(repo_state_dir=tmp_path)
    ids = {b.id for b in bundles}
    assert PRODUCT_FIXTURE_ID in ids
    assert "gb-repo-extra" in ids


def test_load_bundles_product_first_precedence(tmp_path: Path) -> None:
    """Repo-state bundle with same id as product fixture is skipped."""
    _write_bundle(
        tmp_path / "golden_bundles" / "dup.yaml",
        _bundle_dict(id=PRODUCT_FIXTURE_ID, rule="should-not-win"),
    )
    bundles = load_bundles(repo_state_dir=tmp_path)
    # Only one entry for the product fixture id.
    matches = [b for b in bundles if b.id == PRODUCT_FIXTURE_ID]
    assert len(matches) == 1
    # And the product fixture's rule is preserved (ruff-b904), not overridden.
    assert matches[0].rule == "ruff-b904"


# ---------------------------------------------------------------------------
# has_bundle_reference
# ---------------------------------------------------------------------------


def test_has_bundle_reference_true_for_match() -> None:
    bundles = [GoldenBundle.from_dict(_bundle_dict(asset_ref="abc-123"))]
    assert has_bundle_reference("abc-123", bundles) is True


def test_has_bundle_reference_false_for_no_match() -> None:
    bundles = [GoldenBundle.from_dict(_bundle_dict(asset_ref="abc-123"))]
    assert has_bundle_reference("zzz", bundles) is False


def test_has_bundle_reference_empty_list() -> None:
    assert has_bundle_reference("anything", []) is False


# ---------------------------------------------------------------------------
# GoldenBundle.from_dict — forward compatibility
# ---------------------------------------------------------------------------


def test_from_dict_minimal_data_applies_defaults() -> None:
    bundle = GoldenBundle.from_dict({"id": "gb-minimal"})
    assert bundle.id == "gb-minimal"
    assert bundle.asset_class == "pattern"
    assert bundle.asset_ref is None
    assert bundle.rule == ""
    assert bundle.rule_family == ""
    assert bundle.language == ""
    assert bundle.before == ""
    assert bundle.after == ""
    assert bundle.detector_before == ""
    assert bundle.detector_after == ""
    assert bundle.validation_command == ""
    assert bundle.source_finding_id is None
    assert bundle.extracted_at == ""


def test_from_dict_ignores_unknown_keys() -> None:
    bundle = GoldenBundle.from_dict(
        {"id": "gb-x", "future_field": "ignored", "another_unknown": 42}
    )
    assert bundle.id == "gb-x"


# ---------------------------------------------------------------------------
# Fixture validity — quick sanity on the shipped B904 example
# ---------------------------------------------------------------------------


def test_product_fixture_loaded_fields_are_populated() -> None:
    bundles = load_bundles(repo_state_dir=None)
    fixture = next(b for b in bundles if b.id == PRODUCT_FIXTURE_ID)
    assert fixture.rule == "ruff-b904"
    assert fixture.language == "python"
    assert fixture.validation_command == "ruff check --select B904"
    assert "from None" in fixture.after
    assert fixture.detector_after == ""
