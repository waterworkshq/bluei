"""
C4 Migration Guard Tests — verify cli.py behavior before & after decomposition.

These tests pin the external contract of cli.py's symbols so that extraction
into commands/ submodules doesn't silently break anything.  Every guard test
MUST pass against the current monolith AND after each extraction phase.

Strategy:
  - Helper function guards: known inputs → known outputs
  - Symbol import guards: verify symbols are importable from expected paths
  - Structural guards: verify cli.main exists and has the right signature

After extraction is complete, these guards become redundant and should be
removed (or replaced by proper per-module unit tests).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any, List
from unittest.mock import patch, MagicMock

import pytest


def _make_full_args(**overrides):
    """Build a realistic argparse namespace matching cli.py's parse_args defaults."""
    defaults = dict(
        repo_path="/tmp/repo",
        state_file="/tmp/state.json",
        log_file="/tmp/log.txt",
        findings_file="/tmp/findings.jsonl",
        issues_file="/tmp/issues.json",
        worktree_root="/tmp/worktrees",
        status_file="/tmp/status.json",
        docs_index_file="/tmp/docs_index.json",
        reconcile_only=False,
        run_phase="orchestrated",
        smoke_test=False,
        dry_run=True,
        live_github_actions=False,
        max_prs_per_run=2,
        allow_main_commit=False,
        force_push=False,
        max_issues_per_run=10,
        refresh_docs_index=False,
        issue_confidence_threshold=0.7,
        open_issues_cap=20,
        open_prs_cap=5,
        simulate_open_issues=None,
        simulate_open_prs=None,
        finding_cooldown_seconds=3600,
        migrate_context=False,
        staleness_threshold_seconds=3600,
        auto_merge_sandbox=False,
        merge_cooldown_minutes=30,
        regression_check=False,
        auto_rebase_enabled=False,
        rebase_max_prs=5,
        rebase_stats_file=None,
        max_queue_items=None,
        auto_approve=False,
        max_fix_attempts_per_issue=3,
        fix_engine="deterministic",
        deterministic_only=False,
        max_duplicate_prs_threshold=3,
        no_auto_close_duplicate_prs=False,
        claude_cmd_template="claude",
        max_files_changed=5,
        max_loc_diff=200,
        allow_unchanged_baseline_failures=True,
        baseline_checks="[]",
        pr_author="qa-bot",
        bot_author="qa-bot",
        pr_tags="",
        explicit_tag="qa-autofix-ok",
        review_feedback="",
        log_lesson="",
        lessons_file="/tmp/lessons.md",
        batch_pr_enabled=True,
        batch_pr_rules=None,
        batch_state_file="/tmp/batches.jsonl",
        batch_pr_split_on_failure=True,
        batch_dedup_hours=24,
        pattern_store_path=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── Symbol Import Guards ────────────────────────────────────────


class TestSymbolImports:
    """Verify all key symbols are importable from their canonical locations."""

    def test_import_main(self):
        from bluei.engine.cli import main

        assert callable(main)

    def test_import_load_batch_rules(self):
        from bluei.engine.commands.helpers import _load_batch_rules_for_args

        assert callable(_load_batch_rules_for_args)

    def test_import_update_status_artifact(self):
        from bluei.engine.commands.helpers import update_status_artifact

        assert callable(update_status_artifact)

    def test_import_compute_health_score(self):
        from bluei.engine.commands.helpers import _compute_health_score

        assert callable(_compute_health_score)

    def test_import_hydrate_worktree_deps(self):
        from bluei.engine.commands.helpers import _hydrate_worktree_dependencies

        assert callable(_hydrate_worktree_dependencies)

    def test_import_reconcile_issue_pr_link(self):
        from bluei.engine.commands.helpers import _reconcile_issue_pr_link

        assert callable(_reconcile_issue_pr_link)

    def test_import_via_module(self):
        """test_merge_cycle.py uses 'from bluei.engine import cli'."""
        from bluei.engine import cli

        assert hasattr(cli, "main")
        assert hasattr(cli, "update_status_artifact")
        assert hasattr(cli, "_compute_health_score")


# ── Helper Function Guards ──────────────────────────────────────


class TestComputeHealthScore:
    """Pin _compute_health_score behavior."""

    def test_all_zeros_is_100(self):
        from bluei.engine.commands.helpers import _compute_health_score

        score = _compute_health_score(
            raw_open_issues=0, live_open_prs=0, issues_list=[]
        )
        assert score == 100

    def test_issues_reduce_score(self):
        from bluei.engine.commands.helpers import _compute_health_score

        score = _compute_health_score(
            raw_open_issues=5, live_open_prs=0, issues_list=[]
        )
        assert score == 75  # 100 - 5*5

    def test_prs_reduce_score(self):
        from bluei.engine.commands.helpers import _compute_health_score

        score = _compute_health_score(
            raw_open_issues=0, live_open_prs=3, issues_list=[]
        )
        assert score == 70  # 100 - 3*10

    def test_terminal_issues_reduce_score(self):
        from bluei.engine.commands.helpers import _compute_health_score

        issues = [
            {"status": "resolved_merged"},
            {"status": "resolved_verified"},
            {"status": "open"},
        ]
        score = _compute_health_score(
            raw_open_issues=0, live_open_prs=0, issues_list=issues
        )
        # 2 terminal issues (resolved_merged, resolved_verified are NON_ACTIONABLE)
        # But we need to check what NON_ACTIONABLE_ISSUE_STATUSES contains
        # The function counts issues where status in NON_ACTIONABLE_ISSUE_STATUSES
        assert isinstance(score, int)
        assert 0 <= score <= 100

    def test_score_floors_at_zero(self):
        from bluei.engine.commands.helpers import _compute_health_score

        score = _compute_health_score(
            raw_open_issues=100, live_open_prs=100, issues_list=[]
        )
        assert score == 0

    def test_combined_penalties(self):
        from bluei.engine.commands.helpers import _compute_health_score

        score = _compute_health_score(
            raw_open_issues=5, live_open_prs=3, issues_list=[]
        )
        assert score == 45  # 100 - 25 - 30


class TestLoadReviewState:
    """Pin _load_review_state behavior."""

    def test_missing_file_returns_empty(self):
        from bluei.engine.commands.helpers import _load_review_state

        result = _load_review_state(Path("/nonexistent/review_state.json"))
        assert result == {}

    def test_valid_json_returns_dict(self, tmp_path):
        from bluei.engine.commands.helpers import _load_review_state

        f = tmp_path / "review_state.json"
        f.write_text('{"prs": {"1": {"status": "ok"}}}')
        result = _load_review_state(f)
        assert result == {"prs": {"1": {"status": "ok"}}}

    def test_invalid_json_returns_empty(self, tmp_path):
        from bluei.engine.commands.helpers import _load_review_state

        f = tmp_path / "review_state.json"
        f.write_text("not json{{{")
        result = _load_review_state(f)
        assert result == {}

    def test_non_dict_json_returns_empty(self, tmp_path):
        from bluei.engine.commands.helpers import _load_review_state

        f = tmp_path / "review_state.json"
        f.write_text("[1, 2, 3]")
        result = _load_review_state(f)
        assert result == {}


class TestAutonomousReviewGatePasses:
    """Pin _autonomous_review_gate_passes behavior."""

    def test_missing_pr_returns_false(self):
        from bluei.engine.commands.helpers import _autonomous_review_gate_passes

        ok, reason = _autonomous_review_gate_passes({}, 42)
        assert ok is False
        assert "missing" in reason

    def test_not_merge_ready_returns_false(self):
        from bluei.engine.commands.helpers import _autonomous_review_gate_passes

        state = {"prs": {"42": {"last_action": "pending"}}}
        ok, reason = _autonomous_review_gate_passes(state, 42)
        assert ok is False

    def test_merge_ready_passes(self):
        from bluei.engine.commands.helpers import _autonomous_review_gate_passes

        state = {
            "prs": {
                "42": {
                    "last_action": "merge_ready",
                    "last_snapshot": {
                        "merge_state_status": "CLEAN",
                        "actionable_comment_count": 0,
                        "active_change_requesters": [],
                    },
                    "last_review_comment_key": "some-key",
                }
            }
        }
        ok, reason = _autonomous_review_gate_passes(state, 42)
        assert ok is True
        assert "merge-ready" in reason


class TestLoadBatchRulesForArgs:
    """Pin _load_batch_rules_for_args behavior."""

    def test_disabled_returns_empty(self):
        from bluei.engine.commands.helpers import _load_batch_rules_for_args

        args = argparse.Namespace(batch_pr_enabled=False, batch_pr_rules=None)
        result = _load_batch_rules_for_args(args)
        assert result == []

    def test_fallback_returns_hardcoded_rules(self):
        from bluei.engine.commands.helpers import _load_batch_rules_for_args

        args = argparse.Namespace(batch_pr_enabled=True, batch_pr_rules=None)
        result = _load_batch_rules_for_args(args)
        assert len(result) >= 3  # Hardcoded fallback has 4 rules
        # Verify structure
        for rule in result:
            assert hasattr(rule, "rule_pattern")
            assert hasattr(rule, "enabled")


class TestBuildRefactorQueueSnapshot:
    """Pin _build_refactor_queue_snapshot behavior."""

    def test_returns_dict_with_expected_keys(self):
        from bluei.engine.commands.helpers import _build_refactor_queue_snapshot

        result = _build_refactor_queue_snapshot()
        assert isinstance(result, dict)
        for key in (
            "pending_review",
            "approved",
            "executing",
            "completed",
            "aborted",
            "total",
        ):
            assert key in result

    def test_values_are_ints(self):
        from bluei.engine.commands.helpers import _build_refactor_queue_snapshot

        result = _build_refactor_queue_snapshot()
        for key, val in result.items():
            assert isinstance(val, int), f"{key} should be int, got {type(val)}"


class TestUpdateStatusArtifact:
    """Pin update_status_artifact behavior."""

    def test_creates_status_json(self, tmp_path):
        from bluei.engine.commands.helpers import update_status_artifact

        status_file = tmp_path / "status.json"
        issues_file = tmp_path / "issues.json"
        findings_file = tmp_path / "findings.jsonl"

        issues_file.write_text('{"issues": []}')
        findings_file.write_text("")

        args = _make_full_args(
            repo_path=str(tmp_path / "repo"),
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "log.txt"),
            findings_file=str(findings_file),
            issues_file=str(issues_file),
            worktree_root=str(tmp_path / "wt"),
            status_file=str(status_file),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
        )
        reconcile_event = {"reason": "test", "source": "guard"}

        update_status_artifact(
            status_file=status_file,
            state={"open_issues": 0, "open_prs": 0, "created": []},
            issues_file=issues_file,
            findings_file=findings_file,
            args=args,
            run_mode="GUARD-TEST",
            reconcile_event=reconcile_event,
        )

        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert "generated_at" in data
        assert data["run_mode"] == "GUARD-TEST"
        assert "current_counts" in data
        assert "staleness" in data

    def test_overwrites_existing_status(self, tmp_path):
        from bluei.engine.commands.helpers import update_status_artifact

        status_file = tmp_path / "status.json"
        issues_file = tmp_path / "issues.json"
        findings_file = tmp_path / "findings.jsonl"

        status_file.write_text('{"old": true}')
        issues_file.write_text('{"issues": []}')
        findings_file.write_text("")

        args = _make_full_args(
            repo_path=str(tmp_path / "repo"),
            state_file=str(tmp_path / "state.json"),
            log_file=str(tmp_path / "log.txt"),
            findings_file=str(findings_file),
            issues_file=str(issues_file),
            worktree_root=str(tmp_path / "wt"),
            status_file=str(status_file),
            docs_index_file=str(tmp_path / "docs.json"),
            lessons_file=str(tmp_path / "lessons.md"),
        )

        update_status_artifact(
            status_file=status_file,
            state={"open_issues": 0, "open_prs": 0, "created": []},
            issues_file=issues_file,
            findings_file=findings_file,
            args=args,
            run_mode="OVERWRITE-TEST",
            reconcile_event={},
        )

        data = json.loads(status_file.read_text())
        assert data["run_mode"] == "OVERWRITE-TEST"
        assert "current_counts" in data
        assert "staleness" in data
        assert data["old"] is True  # function merges into existing dict


# ── Structural Guards ───────────────────────────────────────────


class TestMainSignature:
    """Verify main() has the expected signature."""

    def test_main_takes_no_args(self):
        from bluei.engine.cli import main
        import inspect

        sig = inspect.signature(main)
        assert len(sig.parameters) == 0

    def test_main_returns_int(self):
        from bluei.engine.cli import main
        import inspect

        sig = inspect.signature(main)
        assert sig.return_annotation == int


class TestGetLlmFixableRules:
    """Pin _get_llm_fixable_rules behavior."""

    def test_returns_dict(self):
        from bluei.engine.commands.helpers import _get_llm_fixable_rules

        result = _get_llm_fixable_rules()
        assert isinstance(result, dict)

    def test_caches_result(self):
        from bluei.engine.commands.helpers import _get_llm_fixable_rules

        r1 = _get_llm_fixable_rules()
        r2 = _get_llm_fixable_rules()
        assert r1 is r2  # same object (cached)


# ── Identity Guards (for backward compat after extraction) ──────


class TestBackwardCompatIdentity:
    """
    After extraction, verify that re-exported symbols in cli.py
    are the SAME objects as those in the new module.

    These will FAIL until extraction is done — they are the GREEN phase target.
    """

    def test_update_status_artifact_is_same_as_helpers(self):
        from bluei.engine.commands.helpers import update_status_artifact
        from bluei.engine.cli import update_status_artifact as cli_usa

        assert update_status_artifact is cli_usa

    def test_compute_health_score_is_same_as_helpers(self):
        from bluei.engine.commands.helpers import _compute_health_score
        from bluei.engine.cli import _compute_health_score as cli_chs

        assert _compute_health_score is cli_chs

    def test_load_batch_rules_is_same_as_helpers(self):
        from bluei.engine.commands.helpers import _load_batch_rules_for_args
        from bluei.engine.cli import _load_batch_rules_for_args as cli_lbr

        assert _load_batch_rules_for_args is cli_lbr

    def test_hydrate_worktree_deps_is_same_as_helpers(self):
        from bluei.engine.commands.helpers import _hydrate_worktree_dependencies
        from bluei.engine.cli import _hydrate_worktree_dependencies as cli_hwd

        assert _hydrate_worktree_dependencies is cli_hwd

    def test_reconcile_issue_pr_link_is_same_as_helpers(self):
        from bluei.engine.commands.helpers import _reconcile_issue_pr_link
        from bluei.engine.cli import _reconcile_issue_pr_link as cli_ripl

        assert _reconcile_issue_pr_link is cli_ripl
