"""Synthetic test for the build-time seed packaging layer (Slice 2a).

Constructs a ParsedRule, calls package_rule into tmp dirs, and verifies the
Bundle YAML round-trips through GoldenBundle.from_dict and the seeded Pattern
JSONL round-trips through FixPattern.from_dict. Also exercises the runtime
loader (load_seeded_patterns) on a tmp seeded_patterns dir.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.pattern_store import FixPattern, FixPatternStore, open_pattern_store
from bluei.engine.seeded_pattern_loader import load_seeded_patterns
from bluei.tools.seed.models import ParsedRule
from bluei.tools.seed.package import package_rule
from bluei.tools.seed.regenerate import regenerate_patterns_from_bundles


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


def test_package_autofix_rule_writes_bundle_and_pattern(tmp_path):
    """alpha.4: broadened gate — all rules with a fix shape produce BOTH
    a Bundle and a seeded Pattern (defense-in-depth; ADR-0019/0020)."""
    parsed = _make_parsed(has_autofix=True)
    manifest = package_rule(
        parsed,
        bundles_dir=tmp_path / "bundles",
        seeded_patterns_dir=tmp_path / "seeded_patterns",
    )
    assert manifest["bundle"] is not None
    assert (
        manifest["pattern"] is not None
    )  # broadened gate: autofix -> Bundle + Pattern


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


_HAS_ESLINT = shutil.which("eslint") is not None
_HAS_RUFF = shutil.which("ruff") is not None
_REAL_BUNDLES = (
    Path(__file__).resolve().parent.parent / "bluei" / "engine" / "golden_bundles"
)


@pytest.mark.skipif(
    not (_HAS_ESLINT and _HAS_RUFF),
    reason="regenerator against real bundles needs ruff + eslint on PATH",
)
def test_regenerate_patterns_from_real_bundles_loads_into_store(tmp_path):
    seeded_dir = tmp_path / "seeded_patterns"
    summary = regenerate_patterns_from_bundles(
        golden_bundles_dir=_REAL_BUNDLES,
        seeded_patterns_dir=seeded_dir,
    )

    assert summary["bundles_read"] == 38
    assert summary["patterns_written"] > 0
    # alpha.4 broadened gate: all rules with a fix shape produce Patterns.
    assert summary["patterns_written"] == 38
    assert summary["skipped_autofixable"] == 0

    # The runtime loader picks the regenerated Patterns up.
    store = FixPatternStore(tmp_path / "patterns.jsonl")
    assert len(store._patterns) == 0
    loaded = load_seeded_patterns(store, seeded_dir=seeded_dir)
    # Two bundles share rule "ruff-b904" (gb-ruff-b904 + ruff-b904-raise-with-from),
    # producing the same pattern_id "seed-ruff-b904" — the store deduplicates by ID.
    assert loaded == summary["patterns_written"]
    assert len(store._patterns) == summary["patterns_written"] - 1  # 37 unique IDs
    # calibration.yaml must be untouched (no *.jsonl collision).
    assert list(seeded_dir.glob("*.jsonl"))


def test_regenerate_patterns_from_synthetic_bundle(tmp_path, monkeypatch):
    """Deterministic unit test: monkeypatch the linter so a synthetic detection-only
    bundle flows through the regenerator and produces a seeded Pattern JSONL line."""
    bundles_dir = tmp_path / "golden_bundles"
    bundles_dir.mkdir()

    bundle_data = {
        "id": "gb-ruff-b904",
        "asset_class": "pattern",
        "asset_ref": None,
        "rule": "ruff-b904",
        "rule_family": "ruff-b",
        "language": "python",
        "before": "try:\n    pass\nexcept Exception:\n    raise\n",
        "after": "try:\n    pass\nexcept Exception:\n    raise from None\n",
        "detector_before": "B904 raise without from",
        "detector_after": "",
        "validation_command": "ruff check --select B904",
        "source_finding_id": None,
        "extracted_at": "2026-06-29T00:00:00Z",
        "imports_touched": [],
        "negative_examples": [],
    }
    (bundles_dir / "gb-ruff-b904.yaml").write_text(
        yaml.safe_dump(bundle_data, sort_keys=False), encoding="utf-8"
    )

    # Stub the linter: confirm validity, force has_autofix=False so the pattern gate fires.
    from bluei.tools.seed import regenerate as regen_mod
    from bluei.tools.seed.validator import ValidationResult

    def _stub_validate(candidate, ruff_executable="ruff", eslint_executable="eslint"):
        candidate.has_autofix = False
        return ValidationResult(
            valid=True,
            before_triggered=True,
            after_clean=True,
            before_diagnostics=1,
            after_diagnostics=0,
            reason="",
        )

    monkeypatch.setattr(regen_mod, "validate_candidate", _stub_validate)

    seeded_dir = tmp_path / "seeded_patterns"
    summary = regenerate_patterns_from_bundles(
        golden_bundles_dir=bundles_dir,
        seeded_patterns_dir=seeded_dir,
    )

    assert summary == {
        "bundles_read": 1,
        "patterns_written": 1,
        "skipped_autofixable": 0,
    }

    pattern_file = seeded_dir / "ruff-b.jsonl"
    assert pattern_file.exists()
    lines = [
        ln for ln in pattern_file.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    pattern = FixPattern.from_dict(record)
    assert pattern.pattern_id == "seed-ruff-b904"
    assert pattern.rule == "ruff-b904"
    assert pattern.rule_family == "ruff-b"
    assert pattern.source == "authoritative-seed"
    assert pattern.confidence == pytest.approx(0.6)
    assert pattern.before_snippet == bundle_data["before"]
    assert pattern.after_snippet == bundle_data["after"]


def test_regenerate_clears_stale_jsonl_but_keeps_calibration(tmp_path, monkeypatch):
    """Regeneration is idempotent: on a successful (all-validated) run,
    pre-existing *.jsonl are cleared, non-jsonl files survive."""
    from bluei.tools.seed import regenerate as regen_mod
    from bluei.tools.seed.validator import ValidationResult

    seeded_dir = tmp_path / "seeded_patterns"
    seeded_dir.mkdir()
    (seeded_dir / "stale.jsonl").write_text("garbage\n", encoding="utf-8")
    (seeded_dir / "calibration.yaml").write_text("key: value\n", encoding="utf-8")

    # Valid stub: the wipe only happens when every bundle validated (D3-06).
    monkeypatch.setattr(
        regen_mod,
        "validate_candidate",
        lambda *a, **k: ValidationResult(
            valid=True,
            before_triggered=True,
            after_clean=True,
            before_diagnostics=1,
            after_diagnostics=0,
            reason="",
        ),
    )
    bundles_dir = tmp_path / "golden_bundles"
    bundles_dir.mkdir()
    (bundles_dir / "gb-ruff-b904.yaml").write_text(
        yaml.safe_dump(
            {
                "rule": "ruff-b904",
                "language": "python",
                "before": "x = 1\n",
                "after": "x = 2\n",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    regenerate_patterns_from_bundles(
        golden_bundles_dir=bundles_dir,
        seeded_patterns_dir=seeded_dir,
    )

    assert not (seeded_dir / "stale.jsonl").exists()
    assert (seeded_dir / "ruff-b.jsonl").exists()  # productive run did the wipe
    assert (seeded_dir / "calibration.yaml").exists()


def _write_bundle(bundles_dir: Path, rule: str, *, after="x = 2\n") -> None:
    (bundles_dir / f"gb-{rule}.yaml").write_text(
        yaml.safe_dump(
            {
                "rule": rule,
                "language": "python",
                "before": "x = 1\n",
                "after": after,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_regenerate_fail_closed_validator_never_deletes_shipped_patterns(
    tmp_path, monkeypatch
):
    """D3-06: when validation fails closed (e.g. ruff/eslint missing from
    PATH), regeneration aborts BEFORE deleting anything — the shipped
    seeded_patterns must survive byte-identical."""
    from bluei.tools.seed import regenerate as regen_mod
    from bluei.tools.seed.regenerate import RegenerationError
    from bluei.tools.seed.validator import ValidationResult

    seeded_dir = tmp_path / "seeded_patterns"
    seeded_dir.mkdir()
    (seeded_dir / "ruff-b.jsonl").write_text(
        '{"pattern_id": "seed-ruff-b904"}\n', encoding="utf-8"
    )
    original = (seeded_dir / "ruff-b.jsonl").read_text(encoding="utf-8")
    (seeded_dir / "calibration.yaml").write_text("key: value\n", encoding="utf-8")

    bundles_dir = tmp_path / "golden_bundles"
    bundles_dir.mkdir()
    _write_bundle(bundles_dir, "ruff-b904")

    monkeypatch.setattr(
        regen_mod,
        "validate_candidate",
        lambda *a, **k: ValidationResult(
            valid=False,
            before_triggered=False,
            after_clean=False,
            before_diagnostics=0,
            after_diagnostics=0,
            reason="linter not on PATH",
        ),
    )

    with pytest.raises(RegenerationError) as excinfo:
        regenerate_patterns_from_bundles(
            golden_bundles_dir=bundles_dir,
            seeded_patterns_dir=seeded_dir,
        )
    assert "gb-ruff-b904" in str(excinfo.value)
    assert "linter not on PATH" in str(excinfo.value)

    # Nothing deleted, nothing written — the shipped dir is untouched.
    assert (seeded_dir / "ruff-b.jsonl").read_text(encoding="utf-8") == original
    assert (seeded_dir / "calibration.yaml").exists()
    assert sorted(p.name for p in seeded_dir.iterdir()) == [
        "calibration.yaml",
        "ruff-b.jsonl",
    ]


def test_regenerate_mixed_valid_and_invalid_writes_nothing(tmp_path, monkeypatch):
    """D3-06: one invalid bundle among valid ones aborts the whole run —
    no wipe, no partial rewrite."""
    from bluei.tools.seed import regenerate as regen_mod
    from bluei.tools.seed.regenerate import RegenerationError
    from bluei.tools.seed.validator import ValidationResult

    seeded_dir = tmp_path / "seeded_patterns"
    seeded_dir.mkdir()
    (seeded_dir / "ruff-b.jsonl").write_text(
        '{"pattern_id": "seed-ruff-b904"}\n', encoding="utf-8"
    )

    bundles_dir = tmp_path / "golden_bundles"
    bundles_dir.mkdir()
    _write_bundle(bundles_dir, "ruff-b904")  # would validate
    _write_bundle(bundles_dir, "ruff-b017")

    def _validate(candidate, ruff_executable="ruff", eslint_executable="eslint"):
        valid = candidate.rule == "ruff-b904"
        return ValidationResult(
            valid=valid,
            before_triggered=valid,
            after_clean=valid,
            before_diagnostics=1 if valid else 0,
            after_diagnostics=0,
            reason="" if valid else "bundle drifted",
        )

    monkeypatch.setattr(regen_mod, "validate_candidate", _validate)

    with pytest.raises(RegenerationError) as excinfo:
        regenerate_patterns_from_bundles(
            golden_bundles_dir=bundles_dir,
            seeded_patterns_dir=seeded_dir,
        )
    assert "gb-ruff-b017" in str(excinfo.value)

    # The VALID bundle must not have been written either (all-or-nothing).
    assert (seeded_dir / "ruff-b.jsonl").read_text(encoding="utf-8") == (
        '{"pattern_id": "seed-ruff-b904"}\n'
    )
    assert sorted(p.name for p in seeded_dir.iterdir()) == ["ruff-b.jsonl"]


def test_regenerate_all_valid_but_zero_patterns_keeps_existing_files(
    tmp_path, monkeypatch
):
    """D3-06: a validated run that would write zero Patterns (all bundles
    without a fix shape) must not wipe the existing seeded patterns."""
    from bluei.tools.seed import regenerate as regen_mod
    from bluei.tools.seed.validator import ValidationResult

    seeded_dir = tmp_path / "seeded_patterns"
    seeded_dir.mkdir()
    (seeded_dir / "ruff-b.jsonl").write_text(
        '{"pattern_id": "seed-ruff-b904"}\n', encoding="utf-8"
    )

    bundles_dir = tmp_path / "golden_bundles"
    bundles_dir.mkdir()
    _write_bundle(bundles_dir, "ruff-b904", after=None)  # no fix shape

    monkeypatch.setattr(
        regen_mod,
        "validate_candidate",
        lambda *a, **k: ValidationResult(
            valid=True,
            before_triggered=True,
            after_clean=True,
            before_diagnostics=1,
            after_diagnostics=0,
            reason="",
        ),
    )

    summary = regenerate_patterns_from_bundles(
        golden_bundles_dir=bundles_dir,
        seeded_patterns_dir=seeded_dir,
    )

    assert summary == {
        "bundles_read": 1,
        "patterns_written": 0,
        "skipped_autofixable": 1,
    }
    # Existing pattern file survives the no-write run.
    assert (seeded_dir / "ruff-b.jsonl").read_text(encoding="utf-8") == (
        '{"pattern_id": "seed-ruff-b904"}\n'
    )


class TestRegenerateScriptExitCodes:
    """D3-06 script wrapper: regeneration failures exit non-zero (never a
    silent wipe-and-exit-0). The script is loaded by path and its shipped
    dirs are redirected to tmp_path — no shipped fixture is touched."""

    @staticmethod
    def _load_script():
        import importlib.util

        script_path = (
            Path(__file__).resolve().parent.parent
            / "scripts"
            / "regenerate_seeded_patterns.py"
        )
        spec = importlib.util.spec_from_file_location(
            "regenerate_seeded_patterns_script", script_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _prepare(self, tmp_path, monkeypatch, *, valid: bool, after="x = 2\n"):
        from bluei.tools.seed import regenerate as regen_mod
        from bluei.tools.seed.validator import ValidationResult

        script = self._load_script()
        bundles_dir = tmp_path / "golden_bundles"
        bundles_dir.mkdir()
        _write_bundle(bundles_dir, "ruff-b904", after=after)
        seeded_dir = tmp_path / "seeded_patterns"
        seeded_dir.mkdir()
        (seeded_dir / "ruff-b.jsonl").write_text(
            '{"pattern_id": "seed-ruff-b904"}\n', encoding="utf-8"
        )
        monkeypatch.setattr(script, "GOLDEN_BUNDLES_DIR", bundles_dir)
        monkeypatch.setattr(script, "SEEDED_PATTERNS_DIR", seeded_dir)
        monkeypatch.setattr(
            regen_mod,
            "validate_candidate",
            lambda *a, **k: ValidationResult(
                valid=valid,
                before_triggered=valid,
                after_clean=valid,
                before_diagnostics=1 if valid else 0,
                after_diagnostics=0,
                reason="" if valid else "linter not on PATH",
            ),
        )
        return script, seeded_dir

    def test_validation_failure_exits_nonzero_without_deleting(
        self, tmp_path, monkeypatch, capsys
    ):
        script, seeded_dir = self._prepare(tmp_path, monkeypatch, valid=False)
        assert script.main() == 1
        assert "gb-ruff-b904" in capsys.readouterr().err
        assert (seeded_dir / "ruff-b.jsonl").exists()  # untouched

    def test_zero_writes_exits_nonzero_without_deleting(
        self, tmp_path, monkeypatch, capsys
    ):
        script, seeded_dir = self._prepare(
            tmp_path, monkeypatch, valid=True, after=None
        )
        assert script.main() == 1
        assert "wrote 0 patterns" in capsys.readouterr().err
        assert (seeded_dir / "ruff-b.jsonl").exists()  # untouched

    def test_productive_run_exits_zero(self, tmp_path, monkeypatch):
        script, seeded_dir = self._prepare(tmp_path, monkeypatch, valid=True)
        assert script.main() == 0
        lines = [
            ln
            for ln in (seeded_dir / "ruff-b.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 1
        assert json.loads(lines[0])["pattern_id"] == "seed-ruff-b904"
