"""Tests for bluei.engine.sprt — two-sided SPRT demotion / re-promotion.

Per ADR-0012 and Phase 7 (PROMPT-08). The LLR is recomputed from durable
stores (dry_replay.jsonl + approval_records.jsonl); there is no stored
accumulator. MISS outcomes do not contribute.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from bluei.engine.sprt import (
    check_sprt,
    compute_llr,
    run_sprt_check,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def _dr_record(
    *,
    pattern_id: str,
    outcome: str,
    timestamp: str,
    finding_id: str = "f1",
    run_id: str = "r1",
    rule: str = "x",
) -> dict:
    return {
        "finding_id": finding_id,
        "pattern_id": pattern_id,
        "rule": rule,
        "run_id": run_id,
        "timestamp": timestamp,
        "validation_commands_passed": [],
        "would_have_outcome": outcome,
    }


# ---------------------------------------------------------------------------
# compute_llr
# ---------------------------------------------------------------------------


def test_compute_llr_hand_computed_value(tmp_path: Path):
    """3 HIT + 7 FAIL with defaults:
    llr = 3*ln(0.9/0.5) + 7*ln(0.1/0.5) = 3*0.5878 + 7*(-1.6094)
        ≈ 1.7634 - 11.2661 ≈ -9.5027
    """
    dr = tmp_path / "dry_replay.jsonl"
    recs = [
        _dr_record(
            pattern_id="p1", outcome="HIT", timestamp=f"2024-01-0{i + 1}T00:00:00Z"
        )
        for i in range(3)
    ] + [
        _dr_record(
            pattern_id="p1", outcome="FAILURE", timestamp=f"2024-02-0{i + 1}T00:00:00Z"
        )
        for i in range(7)
    ]
    _write_jsonl(dr, recs)

    llr, hits, fails = compute_llr("p1", dr, [], p_healthy=0.90, p_broken=0.50)

    assert hits == 3
    assert fails == 7
    expected = 3 * math.log(0.9 / 0.5) + 7 * math.log(0.1 / 0.5)
    assert llr == pytest.approx(expected, rel=1e-9)
    assert llr == pytest.approx(-9.50, abs=0.01)


def test_compute_llr_miss_does_not_contribute(tmp_path: Path):
    dr = tmp_path / "dry_replay.jsonl"
    recs = [
        _dr_record(pattern_id="p1", outcome="HIT", timestamp="2024-01-01T00:00:00Z"),
        _dr_record(pattern_id="p1", outcome="MISS", timestamp="2024-01-02T00:00:00Z"),
        _dr_record(pattern_id="p1", outcome="MISS", timestamp="2024-01-03T00:00:00Z"),
        _dr_record(
            pattern_id="p1", outcome="FAILURE", timestamp="2024-01-04T00:00:00Z"
        ),
    ]
    _write_jsonl(dr, recs)

    llr, hits, fails = compute_llr("p1", dr, [])

    assert hits == 1
    assert fails == 1
    expected = math.log(0.9 / 0.5) + math.log(0.1 / 0.5)
    assert llr == pytest.approx(expected, rel=1e-9)


def test_compute_llr_filters_other_patterns(tmp_path: Path):
    dr = tmp_path / "dry_replay.jsonl"
    recs = [
        _dr_record(pattern_id="p1", outcome="HIT", timestamp="2024-01-01T00:00:00Z"),
        _dr_record(pattern_id="p2", outcome="HIT", timestamp="2024-01-02T00:00:00Z"),
        _dr_record(
            pattern_id="p2", outcome="FAILURE", timestamp="2024-01-03T00:00:00Z"
        ),
    ]
    _write_jsonl(dr, recs)

    llr, hits, fails = compute_llr("p2", dr, [])

    assert hits == 1
    assert fails == 1


def test_compute_llr_missing_file_returns_zero(tmp_path: Path):
    dr = tmp_path / "nope.jsonl"
    llr, hits, fails = compute_llr("p1", dr, [])
    assert llr == 0.0
    assert hits == 0
    assert fails == 0


def test_compute_llr_reset_boundary_excludes_earlier_records(tmp_path: Path):
    """Records before the most recent auto_demote for this asset are excluded."""
    dr = tmp_path / "dry_replay.jsonl"
    approval = [
        # auto_demote at t5 should be the reset boundary
        {
            "asset_ref": "pattern:p1",
            "decision": "auto_demote",
            "timestamp": "2024-01-05T00:00:00Z",
        },
    ]
    recs = [
        # before boundary — must be excluded
        _dr_record(pattern_id="p1", outcome="HIT", timestamp="2024-01-01T00:00:00Z"),
        _dr_record(pattern_id="p1", outcome="HIT", timestamp="2024-01-02T00:00:00Z"),
        # at boundary — included (>=)
        _dr_record(pattern_id="p1", outcome="HIT", timestamp="2024-01-05T00:00:00Z"),
        # after boundary — included
        _dr_record(
            pattern_id="p1", outcome="FAILURE", timestamp="2024-01-08T00:00:00Z"
        ),
    ]
    _write_jsonl(dr, recs)

    llr, hits, fails = compute_llr("p1", dr, approval)

    assert hits == 1
    assert fails == 1


# ---------------------------------------------------------------------------
# check_sprt
# ---------------------------------------------------------------------------


def test_check_sprt_promote_boundary():
    # Many HITs → positive LLR → crosses +A → auto_promote (healthy pattern).
    # A = ln(0.99/0.01) ≈ 4.595
    assert check_sprt(5.0) == "auto_promote"
    assert check_sprt(math.log(0.99 / 0.01)) == "auto_promote"


def test_check_sprt_demote_boundary():
    # Many FAILs → negative LLR → crosses -B → auto_demote (broken pattern).
    # B = ln(0.01/0.99) ≈ -4.595
    assert check_sprt(-5.0) == "auto_demote"
    assert check_sprt(math.log(0.01 / 0.99)) == "auto_demote"


def test_check_sprt_no_boundary():
    assert check_sprt(0.0) is None
    assert check_sprt(2.0) is None
    assert check_sprt(-2.0) is None


def test_check_sprt_custom_alpha_beta():
    # alpha=0.05, beta=0.10 -> A=ln(0.90/0.05)=2.890, B=ln(0.10/0.95)=-2.251
    # High LLR (>=A) → auto_promote (healthy); low LLR (<=B) → auto_demote (broken).
    A = math.log(0.90 / 0.05)
    B = math.log(0.10 / 0.95)
    assert check_sprt(A + 0.5, alpha=0.05, beta=0.10) == "auto_promote"
    assert check_sprt(B - 0.5, alpha=0.05, beta=0.10) == "auto_demote"
    assert check_sprt(0.0, alpha=0.05, beta=0.10) is None


# ---------------------------------------------------------------------------
# run_sprt_check
# ---------------------------------------------------------------------------


def test_run_sprt_check_only_crossing_patterns_produce_records(tmp_path: Path):
    """One pattern with all HITs (promote), one with all FAILs (demote),
    one in-between (no record)."""
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])

    # 20 hits → llr ≈ 20*ln(0.9/0.5) ≈ 11.76 → crosses +A → auto_promote
    # 20 fails → llr ≈ 20*ln(0.1/0.5) ≈ -32.19 → crosses -B → auto_demote
    # 1 hit + 1 fail → llr ≈ 0.588-1.609 = -1.02 → no boundary
    recs = []
    for i in range(20):
        recs.append(
            _dr_record(
                pattern_id="all_hits",
                outcome="HIT",
                timestamp=f"2024-01-{i + 1:02d}T00:00:00Z",
            )
        )
    for i in range(20):
        recs.append(
            _dr_record(
                pattern_id="all_fails",
                outcome="FAILURE",
                timestamp=f"2024-02-{i + 1:02d}T00:00:00Z",
            )
        )
    recs.append(
        _dr_record(pattern_id="mixed", outcome="HIT", timestamp="2024-03-01T00:00:00Z")
    )
    recs.append(
        _dr_record(
            pattern_id="mixed", outcome="FAILURE", timestamp="2024-03-02T00:00:00Z"
        )
    )
    _write_jsonl(dr, recs)

    result = run_sprt_check(["all_hits", "all_fails", "mixed"], dr, ar, config={})

    assert len(result) == 2
    by_pattern = {r["asset_ref"]: r for r in result}
    assert by_pattern["pattern:all_hits"]["decision"] == "auto_promote"
    assert by_pattern["pattern:all_fails"]["decision"] == "auto_demote"
    assert "pattern:mixed" not in by_pattern


def test_run_sprt_check_record_format(tmp_path: Path):
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(
        dr,
        [
            _dr_record(
                pattern_id="p1",
                outcome="HIT",
                timestamp="2024-01-0{i}T00:00:00Z".format(i=i),
            )
            for i in range(1, 21)
        ],
    )

    [record] = run_sprt_check(["p1"], dr, ar, config={})

    assert record["asset_ref"] == "pattern:p1"
    assert record["decision"] == "auto_promote"
    assert record["native_state_before"] == "paused"
    assert record["native_state_after"] == "active"
    assert record["actor"] == "system:sprt"
    assert record["timestamp"]
    assert record["reason"].startswith("SPRT: LLR=")
    assert "hits=20" in record["reason"]
    assert "fails=0" in record["reason"]

    snap = record["evidence_snapshot"]
    assert set(snap.keys()) == {
        "llr",
        "hits",
        "fails",
        "A",
        "B",
        "alpha",
        "beta",
        "p_healthy",
        "p_broken",
    }
    assert snap["hits"] == 20
    assert snap["fails"] == 0
    assert snap["A"] == pytest.approx(math.log(0.99 / 0.01))
    assert snap["B"] == pytest.approx(math.log(0.01 / 0.99))
    assert snap["alpha"] == 0.01
    assert snap["beta"] == 0.01
    assert snap["p_healthy"] == 0.90
    assert snap["p_broken"] == 0.50


def test_run_sprt_check_custom_params(tmp_path: Path):
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    # With alpha=0.5, beta=0.5: A = ln(0.5/0.5)=0, B = ln(0.5/0.5)=0
    # Any LLR >= 0 promotes; any LLR <= 0 demotes. Edge case: llr=0 hits both,
    # but >= is checked first so 0 → promote.
    _write_jsonl(
        dr,
        [_dr_record(pattern_id="p1", outcome="HIT", timestamp="2024-01-01T00:00:00Z")],
    )

    [record] = run_sprt_check(
        ["p1"], dr, ar, config={"learning": {"sprt": {"alpha": 0.5, "beta": 0.5}}}
    )
    assert record["evidence_snapshot"]["alpha"] == 0.5
    assert record["evidence_snapshot"]["beta"] == 0.5


def test_run_sprt_check_empty_pattern_ids(tmp_path: Path):
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(dr, [])
    assert run_sprt_check([], dr, ar, config={}) == []


def test_run_sprt_check_skips_none_pattern_ids(tmp_path: Path):
    """Defensive: a None pattern_id in the list is skipped, not crashed on."""
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(dr, [])
    assert run_sprt_check(["", None], dr, ar, config={}) == []


def test_run_sprt_check_missing_dry_replay_file(tmp_path: Path):
    dr = tmp_path / "missing.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    # No records → llr=0 → 0 >= A is False, 0 <= B is False → no decision.
    assert run_sprt_check(["p1"], dr, ar, config={}) == []


# ---------------------------------------------------------------------------
# run_sprt_check — per-pattern family calibration (pattern_families threading)
# ---------------------------------------------------------------------------


@pytest.fixture
def _calibrated_families(tmp_path: Path, monkeypatch):
    """Point sprt's inline calibration loader at a tmp yaml with a sane
    entry, an equal-values entry (0.5/0.5), and an extremes entry (1.0/0.0) —
    all flagged non-degenerate so only the sanity guard distinguishes them."""
    from bluei.engine import sprt as sprt_mod

    calib = tmp_path / "calibration.yaml"
    calib.write_text(
        "families:\n"
        "  fam-calibrated:\n"
        "    p_healthy: 0.95\n"
        "    p_broken: 0.20\n"
        "    degenerate: false\n"
        "  fam-equal:\n"
        "    p_healthy: 0.5\n"
        "    p_broken: 0.5\n"
        "    degenerate: false\n"
        "  fam-extremes:\n"
        "    p_healthy: 1.0\n"
        "    p_broken: 0.0\n"
        "    degenerate: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sprt_mod, "_CALIBRATION_PATH", calib)
    monkeypatch.setattr(sprt_mod, "_CALIBRATION_CACHE", None)
    monkeypatch.setattr(sprt_mod, "_CALIBRATION_MTIME", None)
    return sprt_mod


def _twenty_hits(pattern_id: str, month: str) -> list[dict]:
    return [
        _dr_record(
            pattern_id=pattern_id,
            outcome="HIT",
            timestamp=f"2024-{month}-{i + 1:02d}T00:00:00Z",
        )
        for i in range(20)
    ]


def test_run_sprt_check_per_pattern_family_calibration(
    tmp_path: Path, _calibrated_families
):
    """Two patterns from different families in ONE call: the pattern whose
    family has a sane calibration entry carries the calibrated
    p_healthy/p_broken in its evidence_snapshot; the other keeps defaults."""
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(
        dr,
        _twenty_hits("p-cal", "01") + _twenty_hits("p-plain", "02"),
    )

    result = run_sprt_check(
        ["p-cal", "p-plain"],
        dr,
        ar,
        config={},
        pattern_families={"p-cal": "fam-calibrated", "p-plain": "fam-other"},
    )

    by_pattern = {r["asset_ref"]: r for r in result}
    assert len(result) == 2

    snap_cal = by_pattern["pattern:p-cal"]["evidence_snapshot"]
    assert by_pattern["pattern:p-cal"]["decision"] == "auto_promote"
    assert snap_cal["p_healthy"] == pytest.approx(0.95)
    assert snap_cal["p_broken"] == pytest.approx(0.20)
    assert snap_cal["llr"] == pytest.approx(20 * math.log(0.95 / 0.20))

    snap_plain = by_pattern["pattern:p-plain"]["evidence_snapshot"]
    assert snap_plain["p_healthy"] == pytest.approx(0.90)
    assert snap_plain["p_broken"] == pytest.approx(0.50)
    assert snap_plain["llr"] == pytest.approx(20 * math.log(0.90 / 0.50))


def test_run_sprt_check_equal_calibration_falls_back_to_defaults(
    tmp_path: Path, _calibrated_families
):
    """A 0.5/0.5 entry (flagged non-degenerate) zeroes every LLR contribution
    (ln(1)=0) — the sanity guard must treat it as degenerate → defaults."""
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(dr, _twenty_hits("p1", "01"))

    [record] = run_sprt_check(
        ["p1"], dr, ar, config={}, pattern_families={"p1": "fam-equal"}
    )

    snap = record["evidence_snapshot"]
    assert record["decision"] == "auto_promote"
    assert snap["p_healthy"] == pytest.approx(0.90)
    assert snap["p_broken"] == pytest.approx(0.50)
    assert snap["llr"] == pytest.approx(20 * math.log(0.90 / 0.50))


def test_run_sprt_check_extreme_calibration_falls_back_no_exception(
    tmp_path: Path, _calibrated_families
):
    """A 1.0/0.0 entry makes the LLR formula divide by zero (0.9/0.0) — the
    sanity guard must route it to defaults without raising."""
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(dr, _twenty_hits("p1", "01"))

    [record] = run_sprt_check(
        ["p1"], dr, ar, config={}, pattern_families={"p1": "fam-extremes"}
    )

    snap = record["evidence_snapshot"]
    assert snap["p_healthy"] == pytest.approx(0.90)
    assert snap["p_broken"] == pytest.approx(0.50)


def test_run_sprt_check_huge_int_calibration_falls_back_no_exception(
    tmp_path: Path, monkeypatch
):
    """A calibration entry whose value is an int beyond float range
    (10**400 — YAML parses it as an arbitrary-precision int; float() then
    raises OverflowError, which is NOT a ValueError) must fall back to
    defaults without raising."""
    from bluei.engine import sprt as sprt_mod

    calib = tmp_path / "calibration.yaml"
    calib.write_text(
        "families:\n"
        "  fam-huge:\n"
        f"    p_healthy: {10**400}\n"
        "    p_broken: 0.20\n"
        "    degenerate: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sprt_mod, "_CALIBRATION_PATH", calib)
    monkeypatch.setattr(sprt_mod, "_CALIBRATION_CACHE", None)
    monkeypatch.setattr(sprt_mod, "_CALIBRATION_MTIME", None)

    # Direct unit level: _sprt_params falls back to defaults, no exception.
    params = sprt_mod._sprt_params({}, "fam-huge")
    assert params["p_healthy"] == pytest.approx(0.90)
    assert params["p_broken"] == pytest.approx(0.50)

    # End to end through run_sprt_check.
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(dr, _twenty_hits("p1", "01"))

    [record] = run_sprt_check(
        ["p1"], dr, ar, config={}, pattern_families={"p1": "fam-huge"}
    )

    snap = record["evidence_snapshot"]
    assert snap["p_healthy"] == pytest.approx(0.90)
    assert snap["p_broken"] == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Integration guard (T7.3)
# ---------------------------------------------------------------------------


def test_integration_guard_active_only(tmp_path: Path, monkeypatch):
    """The SPRT integration in run_pr_cycle_phase only runs when
    learning_mode == 'active'. We verify the guard condition directly.

    Importing run_pr_cycle_phase requires a heavy RunContext; instead we
    inspect the source to confirm the guard is present and correct.
    """
    import inspect

    from bluei.engine.commands import pr_cycle

    src = inspect.getsource(pr_cycle.run_pr_cycle_phase)
    # Guard is exactly as written in PROMPT-08 T7.3
    assert 'ctx.learning_mode == "active"' in src
    assert "run_sprt_check" in src
    # And it's gated on dry_replay.jsonl existence
    assert '"dry_replay.jsonl"' in src


def test_pr_cycle_sprt_wires_pattern_families(tmp_path, monkeypatch):
    """The end-of-cycle SPRT block threads per-pattern families from the
    pattern store in scope (ctx.pattern_store) into run_sprt_check.

    Source-pinning idiom (as in test_integration_guard_active_only): driving
    run_pr_cycle_phase end-to-end requires a heavy RunContext. The behavioral
    half below runs run_sprt_check with the exact map the call site builds —
    stored rule_family when the pattern exists, None when missing.
    """
    import inspect
    import math as _math
    from types import SimpleNamespace

    from bluei.engine.commands import pr_cycle
    from bluei.engine.sprt import run_sprt_check as _real_run

    src = inspect.getsource(pr_cycle.run_pr_cycle_phase)
    assert "pattern_families=" in src, (
        "run_pr_cycle_phase must pass pattern_families to run_sprt_check"
    )
    assert "ctx.pattern_store" in src
    assert "rule_family" in src

    # Behavioral half: the map shape the call site builds
    # (store.get_pattern(pid).rule_family or None, guarded for missing).
    store = SimpleNamespace(
        get_pattern=lambda pid: {
            "p-seeded": SimpleNamespace(rule_family="synthetic-raise"),
            "p-gone-missing": None,
            "p-empty-family": SimpleNamespace(rule_family=""),
        }.get(pid)
    )
    families = {}
    for pid in ("p-seeded", "p-gone-missing", "p-empty-family"):
        pat = store.get_pattern(pid)
        families[pid] = (getattr(pat, "rule_family", "") or None) if pat else None
    assert families == {
        "p-seeded": "synthetic-raise",
        "p-gone-missing": None,
        "p-empty-family": None,
    }

    # And run_sprt_check accepts that map (defaults for None families).
    dr = tmp_path / "dry_replay.jsonl"
    ar = tmp_path / "approval_records.jsonl"
    _write_jsonl(ar, [])
    _write_jsonl(dr, _twenty_hits("p-empty-family", "01"))
    [record] = _real_run(
        ["p-empty-family"], dr, ar, config={}, pattern_families=families
    )
    assert record["evidence_snapshot"]["p_healthy"] == pytest.approx(0.90)
    assert _math.isfinite(record["evidence_snapshot"]["llr"])

    # Wiring seam (tightened): drive run_pr_cycle_phase itself — empty issue
    # queue, a dry_replay.jsonl next to the state file referencing a stored
    # Pattern whose rule_family is set — with run_sprt_check patched at its
    # source module (pr_cycle imports it function-locally). The patched
    # check must receive a pattern_families map containing the store's
    # family for that pattern; broken propagation (the map no longer built
    # from ctx.pattern_store) fails here.
    from unittest.mock import patch

    from bluei.engine.commands.context import RunContext
    from bluei.engine.pattern_store import FixPattern, FixPatternStore

    store = FixPatternStore(tmp_path / "patterns.jsonl")
    pid = store.append(
        FixPattern(
            pattern_id="",
            rule="broad-except",
            language="python",
            file_path="src/example.py",
            before_snippet="except:\n    pass",
            after_snippet="except Exception:\n    pass",
            diff_patch="",
            confidence=0.95,
            success_count=0,
            failure_count=0,
            skip_count=0,
            source="claude",
            created_at="2026-01-01T00:00:00+00:00",
            last_used_at=None,
            last_verified_at=None,
            source_finding_ids=["f001"],
            rule_family="synthetic-raise",
        )
    )
    store._rebuild_from_disk()
    assert store.get_pattern(pid) is not None
    assert store.get_pattern(pid).rule_family == "synthetic-raise"

    state_file = tmp_path / "state" / "status.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(
        state_file.parent / "dry_replay.jsonl",
        [_dr_record(pattern_id=pid, outcome="hit", timestamp="2026-10-23T00:00:00+00:00")],
    )

    ctx = RunContext(
        args=SimpleNamespace(
            batch_pr_enabled=False,
            allow_main_commit=False,
            max_prs_per_run=0,
        ),
        repo_path=tmp_path / "repo",
        state_file=state_file,
        log_file=tmp_path / "run.log",
        findings_file=tmp_path / "findings.jsonl",
        worktree_root=tmp_path / "worktrees",
        gh_repo_slug="acme/widget",
        issues_data={"issues": []},
        eligible_findings=[],
        findings=[],
        PER_REPO_BASELINE_CHECKS={},
        pattern_store=store,
        learning_mode="active",
    )

    with (
        patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main"),
        patch(
            "bluei.engine.commands.pr_cycle._select_candidates",
            return_value=([], 0),
        ),
        patch("bluei.app.config.load_global_config", return_value={}),
        patch("bluei.engine.sprt.run_sprt_check", return_value=[]) as mock_sprt,
    ):
        pr_cycle.run_pr_cycle_phase(ctx)

    mock_sprt.assert_called_once()
    wired_families = mock_sprt.call_args.kwargs.get("pattern_families")
    assert wired_families is not None, (
        "run_pr_cycle_phase must thread pattern_families into run_sprt_check"
    )
    assert wired_families.get(pid) == "synthetic-raise", (
        "the store's rule_family for the dry-replayed pattern must propagate"
    )
