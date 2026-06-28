"""Synthetic test for the build-time seed packaging layer (Slice 2a).

Constructs a ParsedRule, calls package_rule into tmp dirs, and verifies the
Bundle YAML round-trips through GoldenBundle.from_dict and the seeded Pattern
JSONL round-trips through FixPattern.from_dict. Also exercises the runtime
loader (load_seeded_patterns) on a tmp seeded_patterns dir.
"""

import json
from pathlib import Path

import pytest
import yaml

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.pattern_store import FixPattern, FixPatternStore, open_pattern_store
from bluei.engine.seeded_pattern_loader import load_seeded_patterns
from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.package import package_rule


def _make_parsed(*, has_autofix: bool = False, after="x = 1\n") -> ParsedRule:
    return ParsedRule(
        rule="ruff-b904",
        language="python",
        before="try:\n    pass\nexcept Exception:\n    raise\n",
        after=after,
        detector_before="B904: raise without from",
        detector_after="",
        negative_examples=["try:\n    pass\n"],
        has_autofix=has_autofix,
        source_linter="ruff",
        validation_command="ruff check --select B904",
        description="Raise with from in except",
    )


def test_package_detection_only_rule_writes_bundle_and_pattern(tmp_path):
    parsed = _make_parsed(has_autofix=False)
    bundles_dir = tmp_path / "bundles"
    seeded_dir = tmp_path / "seeded_patterns"

    manifest = package_rule(
        parsed,
        bundles_dir=bundles_dir,
        seeded_patterns_dir=seeded_dir,
    )

    # Bundle always written
    assert manifest["bundle"] is not None
    assert manifest["pattern"] is not None
    assert manifest["recipe"] is None  # recipe generation intentionally deferred

    # Bundle round-trips through GoldenBundle.from_dict
    bundle_path = Path(manifest["bundle"])
    assert bundle_path.name == "gb-ruff-b904.yaml"
    bundle_raw = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    bundle = GoldenBundle.from_dict(bundle_raw)
    assert bundle.id == "gb-ruff-b904"
    assert bundle.rule == "ruff-b904"
    assert bundle.rule_family == "ruff-b"  # letter-coded: linter prefix + code letter
    assert bundle.language == "python"
    assert bundle.before == parsed.before
    assert bundle.after == parsed.after
    assert bundle.imports_touched == []
    assert bundle.negative_examples == ["try:\n    pass\n"]

    # Pattern JSONL round-trips through FixPattern.from_dict
    pattern_path = Path(manifest["pattern"])
    assert pattern_path.name == "ruff-b.jsonl"
    lines = [
        ln for ln in pattern_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    pattern = FixPattern.from_dict(record)
    assert pattern.pattern_id == "seed-ruff-b904"
    assert pattern.rule == "ruff-b904"
    assert pattern.source == "authoritative-seed"
    assert pattern.confidence == pytest.approx(0.6)
    assert pattern.before_snippet == parsed.before
    assert pattern.after_snippet == parsed.after
    assert pattern.structural_hash  # populated


def test_package_autofix_rule_writes_bundle_only(tmp_path):
    parsed = _make_parsed(has_autofix=True)
    manifest = package_rule(
        parsed,
        bundles_dir=tmp_path / "bundles",
        seeded_patterns_dir=tmp_path / "seeded_patterns",
    )
    assert manifest["bundle"] is not None
    assert manifest["pattern"] is None  # autofix rules -> bundle only


def test_load_seeded_patterns_injects_into_store(tmp_path):
    parsed = _make_parsed(has_autofix=False)
    seeded_dir = tmp_path / "seeded_patterns"
    package_rule(
        parsed,
        bundles_dir=tmp_path / "bundles",
        seeded_patterns_dir=seeded_dir,
    )

    store_path = tmp_path / "repo_patterns.jsonl"
    store = FixPatternStore(store_path)
    assert len(store._patterns) == 0

    count = load_seeded_patterns(store, seeded_dir=seeded_dir)
    assert count == 1
    # Indexed in memory without disk write
    assert len(store._patterns) == 1
    assert "seed-ruff-b904" in store._patterns
    # Repo-local JSONL stays empty (product fixtures are read-only, not written)
    assert not store_path.exists() or store_path.read_bytes() == b""


def test_load_seeded_patterns_returns_zero_when_dir_missing(tmp_path):
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    assert load_seeded_patterns(store, seeded_dir=tmp_path / "does_not_exist") == 0


def test_open_pattern_store_factory_now_loads_seeded(tmp_path, monkeypatch):
    """The factory's deferred import must resolve; loader runs against an empty
    product dir (returns 0) without triggering ImportError."""
    # Redirect the loader's product dir to an empty tmp dir so the real shipped
    # seeded_patterns/ (currently empty) is not relied upon either way.
    import bluei.engine.seeded_pattern_loader as loader_mod

    monkeypatch.setattr(loader_mod, "_PRODUCT_SEEDED_DIR", tmp_path / "empty_seeded")

    store = open_pattern_store(tmp_path / "patterns.jsonl", load_seeded=True)
    assert isinstance(store, FixPatternStore)


def test_load_seeded_patterns_product_wins_on_conflict(tmp_path):
    """A mined pattern with the same structural hash is replaced by the seed."""
    parsed = _make_parsed(has_autofix=False)
    seeded_dir = tmp_path / "seeded_patterns"
    package_rule(
        parsed, bundles_dir=tmp_path / "bundles", seeded_patterns_dir=seeded_dir
    )

    store = FixPatternStore(tmp_path / "patterns.jsonl")
    # Synthesize a mined pattern sharing the seed's structural hash + rule.
    seed_record = json.loads(
        (seeded_dir / "ruff-b.jsonl").read_text(encoding="utf-8").strip()
    )
    mined = FixPattern.from_dict(dict(seed_record))
    mined.pattern_id = "mined-1"
    mined.source = "autofix"  # mined, not authoritative-seed
    store._index_pattern(mined)
    assert "mined-1" in store._patterns

    load_seeded_patterns(store, seeded_dir=seeded_dir)

    # Mined replaced; seed present
    assert "mined-1" not in store._patterns
    assert "seed-ruff-b904" in store._patterns
