"""Slice 5 (T2.1-T2.5): flywheel_ledger aggregation + enrich_health_with_cost fix.

Tests:
- _build_flywheel_ledger block correctness (per-stage counts, rates, divide-by-zero guard)
- cost_tracker.cycle_savings() accumulates and is independent of cycle_total()
- enrich_health_with_cost no longer counts savings rows as invocations
- status.json written by finalize contains top-level flywheel_ledger
- DONE line includes ledger tokens
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.finalize import _build_flywheel_ledger
from bluei.engine.cost_tracker import CostTracker
from bluei.engine.health import enrich_health_with_cost


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _ledger_records():
    """Mixed ledger records for testing."""
    return [
        {"outcome": "resolved_deterministic", "final_stage": "linter"},
        {"outcome": "resolved_deterministic", "final_stage": "linter"},
        {"outcome": "resolved_deterministic", "final_stage": "recipe"},
        {"outcome": "resolved_deterministic", "final_stage": "pattern-replay"},
        {"outcome": "resolved_deterministic", "final_stage": "pattern-replay"},
        {"outcome": "resolved_deterministic", "final_stage": "pattern-replay"},
        {"outcome": "resolved_deterministic", "final_stage": "composite-pattern"},
        {"outcome": "resolved_deterministic", "final_stage": "ast"},
        {"outcome": "resolved_llm"},
        {"outcome": "resolved_llm"},
        {"outcome": "exhausted"},
    ]


def _mock_pattern_store():
    """Mock pattern store with some active patterns."""
    store = MagicMock()
    p1 = MagicMock()
    p1.pattern_id = "fp-1"
    p1.rule = "broad-except"
    p1.failure_count = 5
    p2 = MagicMock()
    p2.pattern_id = "fp-2"
    p2.rule = "unused-import"
    p2.failure_count = 3
    p3 = MagicMock()
    p3.pattern_id = "fp-3"
    p3.rule = "type-any"
    p3.failure_count = 1
    p4 = MagicMock()
    p4.pattern_id = "fp-4"
    p4.rule = "no-assert"
    p4.failure_count = 0
    store.load_active.return_value = [p1, p2, p3, p4]
    return store


def _mock_cost_tracker(cycle_total=0.50, cycle_savings=0.12):
    ct = MagicMock()
    ct.cycle_total.return_value = cycle_total
    ct.cycle_savings.return_value = cycle_savings
    return ct


def _write_jsonl(path: Path, records: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ── _build_flywheel_ledger tests ─────────────────────────────────────────────


class TestBuildFlywheelLedger:
    def test_per_stage_counts(self):
        records = _ledger_records()
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        assert block["resolved_deterministic_by_stage"]["linter"] == 2
        assert block["resolved_deterministic_by_stage"]["recipe"] == 1
        assert block["resolved_deterministic_by_stage"]["pattern-replay"] == 3
        assert block["resolved_deterministic_by_stage"]["composite-pattern"] == 1
        assert block["resolved_deterministic_by_stage"]["ast"] == 1

    def test_coarse_counts(self):
        records = _ledger_records()
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        assert block["findings_attempted"] == 11
        assert block["resolved_deterministic_total"] == 8
        assert block["resolved_llm"] == 2
        assert block["exhausted"] == 1

    def test_pattern_replay_resolutions_subset(self):
        records = _ledger_records()
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        # Only resolving hits with final_stage=pattern-replay
        assert block["pattern_replay_resolutions"] == 3

    def test_rates_format_num_denom_pct(self):
        records = _ledger_records()
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        # 8/11 (72.7%)
        assert block["rates"]["deterministic_resolution"] == "8/11 (72.7%)"
        # 2/11 (18.2%)
        assert block["rates"]["llm_fallback"] == "2/11 (18.2%)"
        # 1/11 (9.1%)
        assert block["rates"]["exhausted"] == "1/11 (9.1%)"

    def test_divide_by_zero_guard(self):
        records = []
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        assert block["findings_attempted"] == 0
        assert block["rates"]["deterministic_resolution"] == "0/0 (n/a)"
        assert block["rates"]["llm_fallback"] == "0/0 (n/a)"
        assert block["rates"]["exhausted"] == "0/0 (n/a)"

    def test_savings_and_cost_from_tracker(self):
        records = _ledger_records()
        ct = _mock_cost_tracker(cycle_total=1.234567, cycle_savings=0.098765)
        block = _build_flywheel_ledger(records, ct, None)

        assert block["savings_usd"] == 0.098765
        assert block["cost_total_usd"] == 1.234567

    def test_pattern_store_snapshot(self):
        records = []
        ct = _mock_cost_tracker()
        store = _mock_pattern_store()
        block = _build_flywheel_ledger(records, ct, store)

        assert block["active_pattern_count"] == 4
        assert len(block["top_failing_patterns"]) == 3
        assert block["top_failing_patterns"][0]["pattern_id"] == "fp-1"
        assert block["top_failing_patterns"][0]["failure_count"] == 5
        assert block["top_failing_patterns"][1]["pattern_id"] == "fp-2"
        assert block["top_failing_patterns"][2]["pattern_id"] == "fp-3"

    def test_top_failing_excludes_zero_failures(self):
        records = []
        ct = _mock_cost_tracker()
        store = _mock_pattern_store()
        block = _build_flywheel_ledger(records, ct, store)

        # fp-4 has failure_count=0, should be excluded
        ids = [p["pattern_id"] for p in block["top_failing_patterns"]]
        assert "fp-4" not in ids

    def test_no_pattern_store(self):
        records = _ledger_records()
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        assert block["active_pattern_count"] == 0
        assert block["top_failing_patterns"] == []

    def test_only_deterministic_outcomes(self):
        records = [
            {"outcome": "resolved_deterministic", "final_stage": "linter"},
            {"outcome": "resolved_deterministic", "final_stage": "recipe"},
        ]
        ct = _mock_cost_tracker()
        block = _build_flywheel_ledger(records, ct, None)

        assert block["findings_attempted"] == 2
        assert block["resolved_deterministic_total"] == 2
        assert block["resolved_llm"] == 0
        assert block["exhausted"] == 0
        assert block["pattern_replay_resolutions"] == 0


# ── cost_tracker.cycle_savings tests ─────────────────────────────────────────


class TestCycleSavings:
    def test_starts_at_zero(self, tmp_path):
        t = CostTracker(log_path=tmp_path / "cost.jsonl")
        assert t.cycle_savings() == 0.0

    def test_accumulates_savings(self, tmp_path):
        t = CostTracker(log_path=tmp_path / "cost.jsonl")
        t.record_pattern_replay_savings("claude-sonnet-4", 0.01, "fp-1", "rule-1")
        t.record_pattern_replay_savings("claude-sonnet-4", 0.02, "fp-2", "rule-2")
        assert t.cycle_savings() == pytest.approx(0.03, rel=1e-6)

    def test_independent_of_cycle_total(self, tmp_path):
        t = CostTracker(log_path=tmp_path / "cost.jsonl")
        t.record_invocation("claude-sonnet-4", input_tokens=1000, output_tokens=500)
        t.record_pattern_replay_savings("claude-sonnet-4", 0.05, "fp-1", "rule-1")
        # cycle_total tracks invocations only
        assert t.cycle_total() > 0
        # cycle_savings tracks savings only
        assert t.cycle_savings() == pytest.approx(0.05, rel=1e-6)
        # They are independent
        assert t.cycle_savings() != t.cycle_total()

    def test_savings_accumulate_even_on_oserror(self, tmp_path):
        log_path = tmp_path / "noperm" / "cost.jsonl"
        log_path.parent.mkdir()
        log_path.parent.chmod(0o444)
        try:
            t = CostTracker(log_path=log_path)
            t.record_pattern_replay_savings("claude-sonnet-4", 0.01, "fp-1", "rule-1")
            # In-memory accumulator still works even if file write fails
            assert t.cycle_savings() == pytest.approx(0.01, rel=1e-6)
        finally:
            log_path.parent.chmod(0o755)


# ── enrich_health_with_cost bug fix tests ─────────────────────────────────────


class TestEnrichHealthWithCostSavingsFix:
    def test_savings_rows_excluded_from_invocation_count(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
                {"cost": 0.05, "model": "gpt-4"},
                {
                    "type": "pattern_replay_savings",
                    "saved_cost": 0.03,
                    "model": "gpt-4",
                    "pattern_id": "fp-1",
                    "rule": "r",
                },
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        # Should be 2 invocations, not 3
        assert result["cost"]["total_invocations"] == 2
        # total_cost should be 0.15 (not include savings)
        assert result["cost"]["total_cost"] == pytest.approx(0.15, rel=1e-6)

    def test_savings_surfaced_separately(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
                {
                    "type": "pattern_replay_savings",
                    "saved_cost": 0.05,
                    "model": "gpt-4",
                    "pattern_id": "fp-1",
                    "rule": "r",
                },
                {
                    "type": "pattern_replay_savings",
                    "saved_cost": 0.03,
                    "model": "gpt-4",
                    "pattern_id": "fp-2",
                    "rule": "r2",
                },
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert result["cost"]["total_invocations"] == 1
        assert "pattern_replay_savings" in result["cost"]
        assert result["cost"]["pattern_replay_savings"]["total_saved"] == pytest.approx(
            0.08, rel=1e-6
        )
        assert result["cost"]["pattern_replay_savings"]["count"] == 2

    def test_no_savings_omits_key(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert "pattern_replay_savings" not in result["cost"]

    def test_only_savings_rows(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {
                    "type": "pattern_replay_savings",
                    "saved_cost": 0.05,
                    "model": "gpt-4",
                    "pattern_id": "fp-1",
                    "rule": "r",
                },
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        # No real invocations
        assert result["cost"]["total_invocations"] == 0
        assert result["cost"]["total_cost"] == 0.0
        assert result["cost"]["pattern_replay_savings"]["total_saved"] == pytest.approx(
            0.05, rel=1e-6
        )

    def test_per_model_excludes_savings(self, tmp_path):
        log_path = tmp_path / "cost_log.jsonl"
        _write_jsonl(
            log_path,
            [
                {"cost": 0.10, "model": "gpt-4"},
                {
                    "type": "pattern_replay_savings",
                    "saved_cost": 0.05,
                    "model": "gpt-4",
                    "pattern_id": "fp-1",
                    "rule": "r",
                },
            ],
        )

        summary = {}
        result = enrich_health_with_cost(summary, cost_log_path=log_path)
        assert result["cost"]["per_model"]["gpt-4"]["count"] == 1
        assert result["cost"]["per_model"]["gpt-4"]["cost"] == pytest.approx(
            0.10, rel=1e-6
        )


# ── finalize integration tests ───────────────────────────────────────────────


class TestFinalizeFlywheelLedger:
    def _run_finalize(self, tmp_path, ledger_records=None, pattern_store=None):
        """Helper to run finalize with ledger data."""
        from bluei.engine.commands.finalize import run_finalize_phase

        ct = MagicMock()
        ct.cycle_total.return_value = 0.50
        ct.cycle_savings.return_value = 0.12
        ct.warned.return_value = False
        ct.exceeded_limit.return_value = False

        args = SimpleNamespace(
            run_phase="pr-cycle",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
        )

        status_file = tmp_path / "status.json"
        status_file.write_text("{}")

        kwargs = dict(
            state_file=tmp_path / "state.json",
            issues_file=tmp_path / "issues.json",
            status_file=status_file,
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            lessons_file=tmp_path / "lessons.md",
            repo_path=tmp_path,
            args=args,
            state={},
            issues_data={"issues": []},
            reconcile_event={},
            previous_last_run_at=None,
            open_issues=0,
            open_prs=0,
            findings=[{"id": "f1"}, {"id": "f2"}],
            written_findings=0,
            created_issues=[],
            suppressed_findings=[],
            blocked_reasons=[],
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            created_prs=0,
            issues_escalated_max_retries=0,
            merge_attempts=0,
            merges_succeeded=0,
            merges_failed=0,
            merged_pr_urls=[],
            claude_invocations=0,
            opencode_invocations=0,
            deterministic_invocations=0,
            cost_tracker=ct,
            cost_log_path=tmp_path / "cost.json",
            gh_repo_slug="acme/widget",
            ledger_records=ledger_records or [],
            pattern_store=pattern_store,
        )

        with patch("bluei.engine.commands.finalize.update_status_artifact"):
            run_finalize_phase(**kwargs)
        return json.loads(status_file.read_text())

    def test_status_json_contains_top_level_flywheel_ledger(self, tmp_path):
        records = _ledger_records()
        status_data = self._run_finalize(tmp_path, ledger_records=records)

        # Must be top-level, not under latest_run_metrics
        assert "flywheel_ledger" in status_data
        assert "findings_attempted" in status_data["flywheel_ledger"]
        assert status_data["flywheel_ledger"]["findings_attempted"] == 11

    def test_flywheel_ledger_not_in_run_metrics(self, tmp_path):
        records = _ledger_records()
        status_data = self._run_finalize(tmp_path, ledger_records=records)

        # flywheel_ledger should NOT be in latest_run_metrics
        if "latest_run_metrics" in status_data:
            assert "flywheel_ledger" not in status_data["latest_run_metrics"]

    @patch("bluei.engine.commands.finalize._append_text")
    def test_done_line_includes_ledger_tokens(self, mock_append, tmp_path, capsys):
        records = _ledger_records()
        self._run_finalize(tmp_path, ledger_records=records)

        captured = capsys.readouterr()
        assert "det=8/11" in captured.out
        assert "llm=2" in captured.out
        assert "replay_hits=3" in captured.out
        assert "saved=$0.1200" in captured.out
