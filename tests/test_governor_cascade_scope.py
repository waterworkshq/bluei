"""CR-5 (AC-P3-5): Governor scoped-locus guarantee (ADR-0022 amendment 1).

The Governor recommendation ledger records ONE row per Finding that reaches the
``resolve_governed_model`` call in ``_process_one_issue`` (pr_cycle.py ~884).
A **cascade-resolved** Finding short-circuits BEFORE that call — the
``if getattr(finding, "refactor_class", "") == "cascade_fix":`` branch at
pr_cycle.py ~792 runs ``apply_cascade_fix`` and, on success, falls through past
the entire ``if not replay_succeeded:`` block (the Governor call lives in the
``else:`` — the LLM/autofix branch at ~831). So:

* cascade-resolved Finding → ZERO governor ledger rows.
* LLM-routed Finding    → ONE governor ledger row.

This is the ADR-0022 scoped-locus guarantee: the Governor observes only the
LLM-bound surface, never the deterministic cascade.
"""

from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.pr_cycle import _process_one_issue, _WorktreeSetup
from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPattern, FixPatternStore
from bluei.engine.verify import VerificationResult


# ── helpers (mirror test_pr_cycle_ledger_wiring.py) ──────────────────────────


def _pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/example.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a\n+++ a\n-except:\n+except Exception:\n",
        "confidence": 0.95,
        "success_count": 0,
        "failure_count": 0,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": ["f001"],
    }
    data.update(overrides)
    return FixPattern(**data)


def _args(**overrides):
    defaults = dict(
        repo_path="/tmp/repo",
        dry_run=True,
        live_github_actions=False,
        allow_main_commit=False,
        max_prs_per_run=2,
        open_prs_cap=5,
        issue_confidence_threshold=0.5,
        max_fix_attempts_per_issue=3,
        batch_pr_enabled=False,
        fix_engine="deterministic",
        deterministic_only=False,
        max_files_changed=5,
        max_loc_diff=200,
        allow_unchanged_baseline_failures=True,
        claude_cmd_template="claude --print",
        pattern_store_path=None,
        batch_state_file="/tmp/batches.jsonl",
        max_duplicate_prs_threshold=3,
        no_auto_close_duplicate_prs=False,
        workspace=None,
        run_phase="pr-cycle",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _finding(**overrides):
    defaults = dict(
        finding_id="f001",
        repo="test-repo",
        path="src/main.py",
        line=42,
        rule="broad-except",
        snippet="except:\n    pass",
        confidence=0.85,
        quick_win=True,
        safe_to_autofix=True,
        fix_attempts=0,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _cost_tracker():
    return MagicMock(
        exceeded_limit=MagicMock(return_value=False),
        cycle_total=MagicMock(return_value=0.0),
        estimate_invocation_cost=MagicMock(return_value=Decimal("0.05")),
        record_pattern_replay_savings=MagicMock(),
        record_invocation=MagicMock(),
    )


def _build_ctx(tmp_path, store, governor_ledger, **ctx_overrides):
    ctx = RunContext(
        args=_args(),
        repo_path=tmp_path / "repo",
        state_file=tmp_path / "state" / "status.json",
        log_file=tmp_path / "run.log",
        findings_file=tmp_path / "findings.jsonl",
        worktree_root=tmp_path / "worktrees",
        cost_tracker=_cost_tracker(),
        pattern_store=store,
        governor_ledger_path=governor_ledger,
        PER_REPO_BASELINE_CHECKS={},
        **ctx_overrides,
    )
    return ctx


@contextmanager
def _neutralize_post_fix():
    """Neutralize GitHub/worktree/post-fix phases so the function returns via
    the 'verified without changes' path (diff_stats→(0,0) +
    verify_fix_closed→closed)."""
    with ExitStack() as stack:
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle._resolve_github", return_value=False)
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle._setup_worktree",
                return_value=_WorktreeSetup(
                    worktree_path=Path("/tmp/wt"), worktree_branch="fix/f001"
                ),
            )
        )
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle.diff_stats", return_value=(0, 0))
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle.verify_fix_closed",
                return_value=VerificationResult(
                    passed=True, is_closed=True, message=""
                ),
            )
        )
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle.mark_finding_activity")
        )
        stack.enter_context(patch("bluei.engine.commands.pr_cycle.set_issue_status"))
        stack.enter_context(patch("bluei.engine.commands.pr_cycle._append_text"))
        stack.enter_context(patch("bluei.engine.commands.pr_cycle.remove_worktree"))
        yield


def _read_ledger(path):
    if not path.exists():
        return []
    import json

    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── CR-5: cascade-resolved → 0 governor rows ─────────────────────────────────


class TestGovernorCascadeScope:
    """ADR-0022 scoped-locus: the Governor ledger records only LLM-bound
    findings; cascade-resolved findings never reach the Governor call."""

    def test_cascade_resolved_yields_zero_governor_rows(self, tmp_path):
        """A cascade-resolved Finding (standalone miss → cascade success)
        short-circuits before the Governor block → 0 ledger rows.

        The branch structure in ``_process_one_issue``:
        ``if not replay_succeeded:`` → ``if refactor_class == "cascade_fix":``
        → ``apply_cascade_fix`` returns True → falls through PAST the
        ``else:`` (LLM/autofix) branch where the Governor call lives
        (pr_cycle.py ~884). The Governor is never entered.
        """
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pid = store.append(_pattern())
        store._rebuild_from_disk()

        governor_ledger = tmp_path / "governor_recommendations.jsonl"
        ctx = _build_ctx(tmp_path, store, governor_ledger)

        finding = _finding(rule="broad-except", refactor_class="cascade_fix")
        issue = {
            "issue_id": "i1",
            "finding_id": "f001",
            "status": "open",
            "rule": "broad-except",
        }

        def fake_cascade(worktree, fnd, log, **kwargs):
            kwargs["ledger_records"].append(
                {
                    "cycle": kwargs.get("cycle"),
                    "finding_id": fnd.finding_id,
                    "rule": fnd.rule,
                    "stages_tried": ["pattern-replay"],
                    "final_stage": "pattern-replay",
                    "outcome": "resolved_deterministic",
                    "via": "cascade",
                    "pattern_id": pid,
                    "latency_ms": 12,
                    "timestamp": "2026-06-26T00:00:00+00:00",
                }
            )
            return True

        with _neutralize_post_fix():
            with (
                patch(
                    "bluei.engine.commands.pr_cycle.try_replay",
                    return_value=(False, None),
                ),
                patch(
                    "bluei.engine.lifecycle.apply_cascade_fix",
                    side_effect=fake_cascade,
                ) as mock_cascade,
            ):
                _process_one_issue(ctx, 0, issue, finding, {})

        # cascade ran and succeeded
        mock_cascade.assert_called_once()
        # Governor ledger: ZERO rows (cascade short-circuited before the call)
        rows = _read_ledger(governor_ledger)
        assert len(rows) == 0

    def test_llm_routed_yields_one_governor_row(self, tmp_path):
        """An LLM-routed Finding (standalone miss → non-cascade → Claude engine)
        reaches the Governor block → 1 ledger row.

        The finding has ``refactor_class`` != ``"cascade_fix"``, so it enters
        the ``else:`` (LLM/autofix) branch. With ``fix_engine="claude"``,
        ``use_claude_engine=True`` and the Governor call at pr_cycle.py ~884
        runs BEFORE ``apply_claude_fix``.
        """
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        store.append(_pattern())
        store._rebuild_from_disk()

        governor_ledger = tmp_path / "governor_recommendations.jsonl"
        ctx = _build_ctx(tmp_path, store, governor_ledger)
        ctx.args = _args(fix_engine="claude")

        finding = _finding(rule="broad-except")  # no refactor_class
        issue = {
            "issue_id": "i1",
            "finding_id": "f001",
            "status": "open",
            "rule": "broad-except",
        }

        with _neutralize_post_fix():
            with (
                patch(
                    "bluei.engine.commands.pr_cycle.try_replay",
                    return_value=(False, None),
                ),
                patch("bluei.engine.lifecycle.apply_cascade_fix") as mock_cascade,
                patch(
                    "bluei.engine.commands.pr_cycle.apply_claude_fix",
                    return_value=(0, "", "/tmp/prompt.txt"),
                ) as mock_claude,
            ):
                _process_one_issue(ctx, 0, issue, finding, {})

        # cascade never ran (non-cascade finding)
        mock_cascade.assert_not_called()
        # claude fix ran (LLM-routed)
        mock_claude.assert_called_once()
        # Governor ledger: exactly ONE row
        rows = _read_ledger(governor_ledger)
        assert len(rows) == 1
        assert rows[0]["finding_id"] == "f001"

    def test_cascade_vs_llm_row_count_contrast(self, tmp_path):
        """Direct contrast: same finding rule, same ctx — cascade path writes
        0 governor rows, LLM path writes 1. Proves the scoped-locus guarantee
        is structural (branch-dependent), not data-dependent."""
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pid = store.append(_pattern())
        store._rebuild_from_disk()

        issue = {
            "issue_id": "i1",
            "finding_id": "f001",
            "status": "open",
            "rule": "broad-except",
        }

        # --- cascade path ---
        cascade_ledger = tmp_path / "cascade_gov.jsonl"
        ctx_c = _build_ctx(tmp_path, store, cascade_ledger)
        finding_c = _finding(rule="broad-except", refactor_class="cascade_fix")

        def fake_cascade(worktree, fnd, log, **kwargs):
            kwargs["ledger_records"].append(
                {
                    "cycle": kwargs.get("cycle"),
                    "finding_id": fnd.finding_id,
                    "rule": fnd.rule,
                    "stages_tried": ["pattern-replay"],
                    "final_stage": "pattern-replay",
                    "outcome": "resolved_deterministic",
                    "via": "cascade",
                    "pattern_id": pid,
                    "latency_ms": 12,
                    "timestamp": "2026-06-26T00:00:00+00:00",
                }
            )
            return True

        with _neutralize_post_fix():
            with (
                patch(
                    "bluei.engine.commands.pr_cycle.try_replay",
                    return_value=(False, None),
                ),
                patch(
                    "bluei.engine.lifecycle.apply_cascade_fix",
                    side_effect=fake_cascade,
                ),
            ):
                _process_one_issue(ctx_c, 0, dict(issue), finding_c, {})

        cascade_rows = _read_ledger(cascade_ledger)
        assert len(cascade_rows) == 0

        # --- LLM path ---
        llm_ledger = tmp_path / "llm_gov.jsonl"
        ctx_l = _build_ctx(tmp_path, store, llm_ledger)
        ctx_l.args = _args(fix_engine="claude")
        finding_l = _finding(rule="broad-except")  # no refactor_class

        with _neutralize_post_fix():
            with (
                patch(
                    "bluei.engine.commands.pr_cycle.try_replay",
                    return_value=(False, None),
                ),
                patch("bluei.engine.lifecycle.apply_cascade_fix"),
                patch(
                    "bluei.engine.commands.pr_cycle.apply_claude_fix",
                    return_value=(0, "", "/tmp/prompt.txt"),
                ),
            ):
                _process_one_issue(ctx_l, 0, dict(issue), finding_l, {})

        llm_rows = _read_ledger(llm_ledger)
        assert len(llm_rows) == 1

        # The scoped-locus contrast
        assert len(cascade_rows) != len(llm_rows)
