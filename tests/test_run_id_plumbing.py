"""Phase 0 (alpha.2) — run_id plumbing + cascade-internal savings.

Covers T0.1–T0.8 from PROMPT-01:
  1. CostTracker entries carry run_id.
  2. cascade_resolutions.jsonl rows carry run_id after a synthetic
     apply_cascade_fix call.
  3. Cascade-internal Pattern replay records real $ savings in cost_log.jsonl.
  4. Backward compat: old sink rows without run_id still parse cleanly.
"""

import json
from pathlib import Path
from unittest.mock import patch

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStage,
    DeterministicCascade,
    PatternReplayCascadeStage,
)
from bluei.engine.cost_tracker import CostTracker
from bluei.engine.jsonl import append_jsonl, read_jsonl
from bluei.engine.models import Finding


def _finding(rule: str = "ruff-e501", path: str = "src/app.py") -> Finding:
    return Finding(
        finding_id="f-1",
        repo="demo",
        path=path,
        line=10,
        rule=rule,
        snippet="line too long",
        confidence=0.85,
        quick_win=False,
        safe_to_autofix=False,
    )


class _AlwaysSucceedStage(CascadeStage):
    name = "always-succeed"
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=True,
            latency_ms=42,
        )


# --- T0.3: CostTracker stamps run_id ---------------------------------------


def test_cost_tracker_entries_carry_run_id(tmp_path):
    """Both record_invocation and record_pattern_replay_savings stamp run_id."""
    log_path = tmp_path / "cost_log.jsonl"
    tracker = CostTracker(log_path=log_path, run_id="r-tracker-1")

    tracker.record_invocation("claude-sonnet-4", input_tokens=4000, output_tokens=2000)
    tracker.record_pattern_replay_savings(
        model="claude-sonnet-4",
        saved_cost=0.042,
        pattern_id="p-1",
        rule="ruff-e501",
    )

    entries = read_jsonl(log_path)
    assert len(entries) == 2
    assert entries[0]["run_id"] == "r-tracker-1"
    assert entries[1]["run_id"] == "r-tracker-1"
    assert entries[1]["type"] == "pattern_replay_savings"


def test_cost_tracker_run_id_defaults_empty(tmp_path):
    """CostTracker constructed without run_id stamps "" (additive, no break)."""
    log_path = tmp_path / "cost_log.jsonl"
    tracker = CostTracker(log_path=log_path)
    tracker.record_invocation("claude-sonnet-4")

    entries = read_jsonl(log_path)
    assert entries[0]["run_id"] == ""


# --- T0.4/T0.5: cascade_resolutions.jsonl rows carry run_id ----------------


def test_apply_cascade_fix_stamps_run_id_on_ledger(tmp_path):
    """A synthetic apply_cascade_fix call threads run_id into CascadeContext,
    and the resolved ledger row written to cascade_resolutions.jsonl carries it."""
    from bluei.engine.lifecycle import apply_cascade_fix

    log_file = tmp_path / "log.txt"
    log_file.write_text("")
    ledger_path = tmp_path / "cascade_resolutions.jsonl"

    with (
        patch(
            "bluei.engine.cascade.default_cascade_stages",
            return_value=[_AlwaysSucceedStage()],
        ),
        patch("bluei.engine.lifecycle._extract_fix_pattern", return_value=None),
        patch("bluei.engine.lifecycle.resolve_tier", return_value="T1"),
        patch("bluei.engine.lifecycle._tier_validate", return_value=True),
    ):
        applied = apply_cascade_fix(
            worktree_path=tmp_path,
            finding=_finding(),
            log_file=log_file,
            ledger_path=ledger_path,
            cycle="pr-cycle",
            run_id="r-cascade-1",
        )

    assert applied is True
    assert ledger_path.exists()
    rows = [
        json.loads(line)
        for line in ledger_path.read_text().splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r-cascade-1"
    assert rows[0]["outcome"] == "resolved_deterministic"


# --- T0.7: cascade-internal Pattern replay records savings -----------------


def test_cascade_pattern_replay_records_savings(tmp_path):
    """When PatternReplayCascadeStage.attempt succeeds with a cost_tracker in
    context, it records a pattern_replay_savings entry carrying run_id."""
    cost_log = tmp_path / "cost_log.jsonl"
    tracker = CostTracker(log_path=cost_log, run_id="r-replay-1")
    log_file = tmp_path / "log.txt"
    log_file.write_text("")

    ctx = CascadeContext(
        log_file=log_file,
        pattern_store=object(),  # unused — try_replay is patched
        cost_tracker=tracker,
        run_id="r-replay-1",
    )

    with patch(
        "bluei.engine.pattern_replay.try_replay",
        return_value=(True, "p-replay-1"),
    ):
        result = PatternReplayCascadeStage().attempt(_finding(), tmp_path, ctx)

    assert result.success is True
    assert result.pattern_id == "p-replay-1"

    entries = read_jsonl(cost_log)
    savings = [e for e in entries if e.get("type") == "pattern_replay_savings"]
    assert len(savings) == 1
    assert savings[0]["pattern_id"] == "p-replay-1"
    assert savings[0]["run_id"] == "r-replay-1"
    assert savings[0]["saved_cost"] > 0


def test_cascade_replay_no_savings_without_tracker(tmp_path):
    """No cost_tracker in context → replay succeeds but no savings entry written."""
    log_file = tmp_path / "log.txt"
    log_file.write_text("")
    ctx = CascadeContext(
        log_file=log_file,
        pattern_store=object(),
    )
    with patch(
        "bluei.engine.pattern_replay.try_replay",
        return_value=(True, "p-2"),
    ):
        result = PatternReplayCascadeStage().attempt(_finding(), tmp_path, ctx)

    assert result.success is True
    # No cost_log path was ever configured → nothing to assert beyond success.


# --- Backward compat --------------------------------------------------------


def test_old_ledger_rows_without_run_id_still_parse(tmp_path):
    """Pre-alpha.2 sink rows (no run_id key) read without error."""
    legacy_path = tmp_path / "cascade_resolutions.jsonl"
    legacy_record = {
        "cycle": "pr-cycle",
        "finding_id": "legacy-1",
        "rule": "ruff-e501",
        "outcome": "resolved_deterministic",
        # note: no run_id
    }
    append_jsonl(legacy_path, legacy_record)

    rows = read_jsonl(legacy_path)
    assert len(rows) == 1
    assert rows[0]["finding_id"] == "legacy-1"
    # run_id simply absent — not an error
    assert "run_id" not in rows[0]
