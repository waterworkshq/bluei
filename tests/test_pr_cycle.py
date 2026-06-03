"""Tests for bluei.engine.commands.pr_cycle — queue building, candidate filtering, batch routing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.pr_cycle import run_pr_cycle_phase
from bluei.engine.models import Finding


from bluei.engine.reforge import RefactorClass


def _finding(**overrides):
    defaults = dict(
        finding_id="f001",
        repo="test-repo",
        path="src/main.py",
        line=42,
        rule="ruff-c408",
        snippet="dict(a=1)",
        confidence=0.85,
        quick_win=True,
        safe_to_autofix=True,
        fix_attempts=0,
    )
    defaults.update(overrides)
    return Finding(**defaults)


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
        claude_cmd_template="",
        pattern_store_path=None,
        batch_state_file="/tmp/batches.jsonl",
        max_duplicate_prs_threshold=3,
        no_auto_close_duplicate_prs=False,
        workspace=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _issue(**overrides):
    defaults = dict(
        issue_id="i1",
        finding_id="f001",
        status="open",
        rule="ruff-c408",
        path="src/main.py",
        line=42,
        confidence=0.85,
        quick_win=True,
        safe_to_autofix=True,
    )
    defaults.update(overrides)
    return defaults


class TestQueueBuilding:
    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch("bluei.engine.commands.pr_cycle.finding_from_issue_record")
    @patch(
        "bluei.engine.commands.pr_cycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch(
        "bluei.engine.commands.pr_cycle.check_finding_escalation_before_fix",
        return_value=False,
    )
    @patch("bluei.engine.commands.pr_cycle.count_failed_fix_attempts", return_value=0)
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    def test_skips_resolved_merged(
        self,
        mock_llm,
        mock_count,
        mock_escalate,
        mock_classify,
        mock_ffir,
        mock_log,
        mock_branch,
        tmp_path,
    ):
        issues_data = {
            "issues": [
                _issue(status="resolved_merged"),
            ]
        }
        mock_ffir.return_value = _finding()

        result = run_pr_cycle_phase(
            repo_path=tmp_path,
            findings_file=tmp_path / "findings.jsonl",
            log_file=tmp_path / "run.log",
            worktree_root=tmp_path / "worktrees",
            gh_repo_slug="acme/widget",
            review_state_file=tmp_path / "review_state.json",
            docs_index_file=tmp_path / "docs.json",
            lessons_file=tmp_path / "lessons.md",
            args=_args(),
            state={},
            issues_data=issues_data,
            eligible_findings=[],
            findings=[],
            PER_REPO_BASELINE_CHECKS={},
            cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
            pattern_store=None,
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
        )
        assert result["fix_attempts"] == 0

    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch("bluei.engine.commands.pr_cycle.finding_from_issue_record")
    @patch(
        "bluei.engine.commands.pr_cycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch(
        "bluei.engine.commands.pr_cycle.check_finding_escalation_before_fix",
        return_value=False,
    )
    @patch("bluei.engine.commands.pr_cycle.count_failed_fix_attempts", return_value=0)
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    def test_skips_issue_with_existing_pr(
        self,
        mock_llm,
        mock_count,
        mock_escalate,
        mock_classify,
        mock_ffir,
        mock_log,
        mock_branch,
        tmp_path,
    ):
        issues_data = {
            "issues": [
                _issue(
                    github={
                        "pr_number": 42,
                        "pr_url": "https://github.com/acme/widget/pull/42",
                    }
                ),
            ]
        }
        mock_ffir.return_value = _finding()

        result = run_pr_cycle_phase(
            repo_path=tmp_path,
            findings_file=tmp_path / "findings.jsonl",
            log_file=tmp_path / "run.log",
            worktree_root=tmp_path / "worktrees",
            gh_repo_slug="acme/widget",
            review_state_file=tmp_path / "review_state.json",
            docs_index_file=tmp_path / "docs.json",
            lessons_file=tmp_path / "lessons.md",
            args=_args(),
            state={},
            issues_data=issues_data,
            eligible_findings=[],
            findings=[],
            PER_REPO_BASELINE_CHECKS={},
            cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
            pattern_store=None,
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
        )
        assert result["fix_attempts"] == 0

    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch(
        "bluei.engine.commands.pr_cycle.finding_from_issue_record", return_value=None
    )
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    def test_skips_issue_with_no_finding(
        self, mock_llm, mock_ffir, mock_log, mock_branch, tmp_path
    ):
        issues_data = {"issues": [_issue()]}

        result = run_pr_cycle_phase(
            repo_path=tmp_path,
            findings_file=tmp_path / "findings.jsonl",
            log_file=tmp_path / "run.log",
            worktree_root=tmp_path / "worktrees",
            gh_repo_slug="acme/widget",
            review_state_file=tmp_path / "review_state.json",
            docs_index_file=tmp_path / "docs.json",
            lessons_file=tmp_path / "lessons.md",
            args=_args(),
            state={},
            issues_data=issues_data,
            eligible_findings=[],
            findings=[],
            PER_REPO_BASELINE_CHECKS={},
            cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
            pattern_store=None,
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
        )
        assert result["fix_attempts"] == 0

    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch("bluei.engine.commands.pr_cycle.finding_from_issue_record")
    @patch(
        "bluei.engine.commands.pr_cycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch(
        "bluei.engine.commands.pr_cycle.check_finding_escalation_before_fix",
        return_value=False,
    )
    @patch("bluei.engine.commands.pr_cycle.count_failed_fix_attempts", return_value=5)
    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    def test_escalates_max_retries_exceeded(
        self,
        mock_llm,
        mock_set,
        mock_count,
        mock_escalate,
        mock_classify,
        mock_ffir,
        mock_log,
        mock_branch,
        tmp_path,
    ):
        issues_data = {"issues": [_issue()]}
        mock_ffir.return_value = _finding()

        result = run_pr_cycle_phase(
            repo_path=tmp_path,
            findings_file=tmp_path / "findings.jsonl",
            log_file=tmp_path / "run.log",
            worktree_root=tmp_path / "worktrees",
            gh_repo_slug="acme/widget",
            review_state_file=tmp_path / "review_state.json",
            docs_index_file=tmp_path / "docs.json",
            lessons_file=tmp_path / "lessons.md",
            args=_args(max_fix_attempts_per_issue=3),
            state={},
            issues_data=issues_data,
            eligible_findings=[],
            findings=[],
            PER_REPO_BASELINE_CHECKS={},
            cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
            pattern_store=None,
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
        )
        assert result["issues_escalated_max_retries"] == 1
        mock_set.assert_called()

    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch("bluei.engine.commands.pr_cycle.finding_from_issue_record")
    @patch(
        "bluei.engine.commands.pr_cycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch(
        "bluei.engine.commands.pr_cycle.check_finding_escalation_before_fix",
        return_value=False,
    )
    @patch("bluei.engine.commands.pr_cycle.count_failed_fix_attempts", return_value=0)
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    def test_skips_low_confidence(
        self,
        mock_llm,
        mock_count,
        mock_escalate,
        mock_classify,
        mock_ffir,
        mock_log,
        mock_branch,
        tmp_path,
    ):
        issues_data = {"issues": [_issue()]}
        mock_ffir.return_value = _finding(confidence=0.3)

        result = run_pr_cycle_phase(
            repo_path=tmp_path,
            findings_file=tmp_path / "findings.jsonl",
            log_file=tmp_path / "run.log",
            worktree_root=tmp_path / "worktrees",
            gh_repo_slug="acme/widget",
            review_state_file=tmp_path / "review_state.json",
            docs_index_file=tmp_path / "docs.json",
            lessons_file=tmp_path / "lessons.md",
            args=_args(issue_confidence_threshold=0.7),
            state={},
            issues_data=issues_data,
            eligible_findings=[],
            findings=[],
            PER_REPO_BASELINE_CHECKS={},
            cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
            pattern_store=None,
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
        )
        assert result["fix_attempts"] == 0

    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch("bluei.engine.commands.pr_cycle.finding_from_issue_record")
    @patch("bluei.engine.commands.pr_cycle.set_issue_status")
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    def test_not_fixable_not_llm_escalates(
        self, mock_llm, mock_set, mock_ffir, mock_log, mock_branch, tmp_path
    ):
        from bluei.engine.reforge import RefactorClass

        issues_data = {"issues": [_issue()]}
        mock_ffir.return_value = _finding(safe_to_autofix=False)

        with patch(
            "bluei.engine.commands.pr_cycle.classify_finding",
            return_value=RefactorClass.SIMPLE_FIX,
        ):
            result = run_pr_cycle_phase(
                repo_path=tmp_path,
                findings_file=tmp_path / "findings.jsonl",
                log_file=tmp_path / "run.log",
                worktree_root=tmp_path / "worktrees",
                gh_repo_slug="acme/widget",
                review_state_file=tmp_path / "review_state.json",
                docs_index_file=tmp_path / "docs.json",
                lessons_file=tmp_path / "lessons.md",
                args=_args(),
                state={},
                issues_data=issues_data,
                eligible_findings=[],
                findings=[],
                PER_REPO_BASELINE_CHECKS={},
                cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
                pattern_store=None,
                created_prs=0,
                open_prs=0,
                fix_attempts=0,
                fixes_verified=0,
                fixes_failed_verification=0,
                issues_escalated_max_retries=0,
                claude_invocations=0,
                deterministic_invocations=0,
                blocked_reasons=[],
            )
        assert result["fix_attempts"] == 0
        status_calls = [
            c for c in mock_set.call_args_list if "needs-human-not-fixable" in str(c)
        ]
        assert len(status_calls) == 1


class TestEmptyQueue:
    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    def test_empty_issues_returns_zero(self, mock_log, mock_branch, tmp_path):
        result = run_pr_cycle_phase(
            repo_path=tmp_path,
            findings_file=tmp_path / "findings.jsonl",
            log_file=tmp_path / "run.log",
            worktree_root=tmp_path / "worktrees",
            gh_repo_slug="acme/widget",
            review_state_file=tmp_path / "review_state.json",
            docs_index_file=tmp_path / "docs.json",
            lessons_file=tmp_path / "lessons.md",
            args=_args(),
            state={},
            issues_data={"issues": []},
            eligible_findings=[],
            findings=[],
            PER_REPO_BASELINE_CHECKS={},
            cost_tracker=MagicMock(exceeded_limit=MagicMock(return_value=False)),
            pattern_store=None,
            created_prs=0,
            open_prs=0,
            fix_attempts=0,
            fixes_verified=0,
            fixes_failed_verification=0,
            issues_escalated_max_retries=0,
            claude_invocations=0,
            deterministic_invocations=0,
            blocked_reasons=[],
        )
        assert result["created_prs"] == 0
        assert result["fix_attempts"] == 0
        log_msgs = [str(c) for c in mock_log.call_args_list]
        assert any("no eligible" in m for m in log_msgs)


class TestBatchRouting:
    @patch("bluei.engine.commands.pr_cycle.get_branch", return_value="main")
    @patch("bluei.engine.commands.pr_cycle._append_text")
    @patch("bluei.engine.commands.pr_cycle.finding_from_issue_record")
    @patch(
        "bluei.engine.commands.pr_cycle.classify_finding",
        return_value=RefactorClass.SIMPLE_FIX,
    )
    @patch(
        "bluei.engine.commands.pr_cycle.check_finding_escalation_before_fix",
        return_value=False,
    )
    @patch("bluei.engine.commands.pr_cycle.count_failed_fix_attempts", return_value=0)
    @patch("bluei.engine.commands.pr_cycle._get_llm_fixable_rules", return_value={})
    @patch("bluei.engine.commands.pr_cycle._load_batch_rules_for_args")
    @patch("bluei.engine.commands.pr_cycle.guard_open_prs", return_value=(True, "ok"))
    def test_batch_enabled_groups_candidates(
        self,
        mock_guard,
        mock_rules,
        mock_llm,
        mock_count,
        mock_escalate,
        mock_classify,
        mock_ffir,
        mock_log,
        mock_branch,
        tmp_path,
    ):
        from bluei.engine.batch_pr import BatchGroup

        f1 = _finding(finding_id="f001", rule="ruff-c408")
        f2 = _finding(finding_id="f002", rule="ruff-c408")
        issues_data = {
            "issues": [
                _issue(issue_id="i1", finding_id="f001"),
                _issue(issue_id="i2", finding_id="f002"),
            ]
        }
        mock_ffir.side_effect = [f1, f2]
        mock_rules.return_value = []

        bg = MagicMock()
        bg.is_solo = False
        bg.issues = [_issue(issue_id="i1"), _issue(issue_id="i2")]
        bg.findings = [f1, f2]
        bg.batch_id = "batch-1"
        bg.to_record.return_value = {"batch_id": "batch-1"}

        with patch("bluei.engine.batch_pr.group_findings_for_batch", return_value=[bg]):
            with patch(
                "bluei.engine.batch_pr.process_batch",
                return_value=(True, "ok"),
            ):
                with patch("bluei.engine.state.save_batch_record"):
                    result = run_pr_cycle_phase(
                        repo_path=tmp_path,
                        findings_file=tmp_path / "findings.jsonl",
                        log_file=tmp_path / "run.log",
                        worktree_root=tmp_path / "worktrees",
                        gh_repo_slug="acme/widget",
                        review_state_file=tmp_path / "review_state.json",
                        docs_index_file=tmp_path / "docs.json",
                        lessons_file=tmp_path / "lessons.md",
                        args=_args(batch_pr_enabled=True),
                        state={},
                        issues_data=issues_data,
                        eligible_findings=[],
                        findings=[],
                        PER_REPO_BASELINE_CHECKS={},
                        cost_tracker=MagicMock(
                            exceeded_limit=MagicMock(return_value=False)
                        ),
                        pattern_store=None,
                        created_prs=0,
                        open_prs=0,
                        fix_attempts=0,
                        fixes_verified=0,
                        fixes_failed_verification=0,
                        issues_escalated_max_retries=0,
                        claude_invocations=0,
                        deterministic_invocations=0,
                        blocked_reasons=[],
                    )
        assert result["created_prs"] >= 1
