"""Behavior-focused tests for bluei.engine.lifecycle public API.

Tests exercise public functions only — private helpers are covered indirectly
through their callers.
"""

import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.fix_tiers import FixTier
from bluei.engine.git_ops import git_commit_all, git_push_branch, diff_stats
from bluei.engine.lifecycle import (
    apply_autofix,
    apply_cascade_fix,
    apply_claude_fix,
    ClaudeFixRequest,
    process_refactor_queue,
    route_to_human_review,
)
from bluei.engine.review_helpers import classify_review_feedback, review_loop_allowed
from bluei.engine.startup import STALE_LOCK_HOURS, run_startup_self_healing
from bluei.engine.validation import (
    build_target_checks,
    choose_validation_baseline,
    run_named_checks,
    run_smoke_test,
    run_validation_gate,
    verify_fix_closed,
)
from bluei.engine.models import Finding
from bluei.engine.reforge import RefactorClass, RefactorPhase, RefactorWork


# --- Helpers ---


def _make_queue_item(work_id: str = "w-1", rule: str = "xo-complexity"):
    item = MagicMock()
    item.work_id = work_id
    item.finding_dict = {
        "finding_id": f"f-{work_id}",
        "repo": "r",
        "path": "p",
        "line": 1,
        "rule": rule,
        "snippet": "s",
        "confidence": 0.9,
        "quick_win": False,
        "safe_to_autofix": False,
    }
    return item


# --- TestApplyClaudeFix ---


class TestApplyClaudeFix:
    """LLM-based fix engine: success, failure, template errors, cleanup."""

    @patch("bluei.engine.lifecycle.subprocess.run")
    def test_success_returns_zero(self, mock_run, tmp_path, make_finding):
        mock_run.return_value = MagicMock(returncode=0, stdout="fixed!")
        log = tmp_path / "log.txt"
        log.write_text("")
        rc, output, _ = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
            ),
        )
        assert rc == 0
        assert output == "fixed!"

    @patch("bluei.engine.lifecycle.subprocess.run")
    def test_failure_returns_nonzero(self, mock_run, tmp_path, make_finding):
        mock_run.return_value = MagicMock(returncode=1, stdout="error details")
        log = tmp_path / "log.txt"
        log.write_text("")
        rc, output, _ = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
            ),
        )
        assert rc == 1
        assert "error details" in output

    def test_invalid_template_placeholder_returns_rc2(self, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.write_text("")
        rc, output, _ = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="claude --print {bad_placeholder}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
            ),
        )
        assert rc == 2
        assert "invalid claude command template placeholder" in output

    @patch("bluei.engine.lifecycle.subprocess.run")
    def test_prompt_file_cleaned_up(self, mock_run, tmp_path, make_finding):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        log = tmp_path / "log.txt"
        log.write_text("")
        _, _, prompt_path = apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
            ),
        )
        assert not Path(prompt_path).exists()

    @patch("bluei.engine.lifecycle.subprocess.run")
    @patch("bluei.engine.lifecycle.append_lesson")
    @patch("bluei.engine.lifecycle.load_lessons_for_finding", return_value=[])
    @patch("bluei.engine.lifecycle.load_lessons_for_rule", return_value=[])
    @patch("bluei.engine.lifecycle.load_failure_clusters_for_rule", return_value="")
    def test_lessons_appended(
        self, mock_cl, mock_rl, mock_lf, mock_lesson, mock_run, tmp_path, make_finding
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        lessons = tmp_path / "LESSONS_LOG.md"
        lessons.write_text("")
        log = tmp_path / "log.txt"
        log.write_text("")
        apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
                lessons_file=lessons,
            ),
        )
        assert mock_lesson.call_count == 1

    @patch("bluei.engine.lifecycle.subprocess.run")
    @patch("bluei.engine.lifecycle.increment_fix_attempt")
    @patch("bluei.engine.lifecycle.update_finding_record")
    def test_findings_file_updated(
        self, mock_update, mock_inc, mock_run, tmp_path, make_finding
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok")
        findings = tmp_path / "findings.jsonl"
        findings.write_text("")
        log = tmp_path / "log.txt"
        log.write_text("")
        apply_claude_fix(
            ClaudeFixRequest(
                worktree_path=tmp_path,
                finding=make_finding(),
                baseline_checks={},
                target_checks={},
                claude_cmd_template="echo {prompt_file}",
                max_files_changed=5,
                max_loc_diff=200,
                log_file=log,
                findings_file=findings,
            ),
        )
        mock_inc.assert_called_once()
        mock_update.assert_called_once()


# --- TestApplyAutofix ---


class TestApplyAutofix:
    """Deterministic fix strategies: recipes, hardcoded patterns, safety gates."""

    @patch(
        "bluei.engine.lifecycle.classify_finding", return_value=RefactorClass.SIMPLE_FIX
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_recipe_match_succeeds(
        self, mock_re, mock_classify, tmp_path, make_finding
    ):
        mock_recipe = MagicMock(id="test-recipe")
        mock_re.match.return_value = mock_recipe
        mock_re.apply.return_value = MagicMock(success=True)
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("x = 1")
        log = tmp_path / "log.txt"
        log.write_text("")
        with (
            patch(
                "bluei.engine.lifecycle.resolve_tier",
                return_value=FixTier.T0_GUARANTEED,
            ),
            patch("bluei.engine.lifecycle._tier_validate", return_value=True),
            patch("bluei.engine.lifecycle._extract_fix_pattern"),
        ):
            assert apply_autofix(tmp_path, make_finding(path="src/app.py"), log) is True

    @patch(
        "bluei.engine.lifecycle.classify_finding", return_value=RefactorClass.SIMPLE_FIX
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_recipe_failure_returns_false(
        self, mock_re, mock_classify, tmp_path, make_finding
    ):
        mock_re.match.return_value = MagicMock(id="test-recipe")
        mock_re.apply.return_value = MagicMock(success=False, error="could not apply")
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("x = 1")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert apply_autofix(tmp_path, make_finding(path="src/app.py"), log) is False

    @patch(
        "bluei.engine.lifecycle.classify_finding", return_value=RefactorClass.SIMPLE_FIX
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_no_recipe_returns_false(
        self, mock_re, mock_classify, tmp_path, make_finding
    ):
        mock_re.match.return_value = None
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("x = 1")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert (
            apply_autofix(
                tmp_path, make_finding(path="src/app.py", rule="unknown"), log
            )
            is False
        )

    @patch(
        "bluei.engine.lifecycle.classify_finding", return_value=RefactorClass.SIMPLE_FIX
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_missing_target_file_returns_false(
        self, mock_re, mock_classify, tmp_path, make_finding
    ):
        log = tmp_path / "log.txt"
        log.write_text("")
        assert (
            apply_autofix(tmp_path, make_finding(path="src/missing.py"), log) is False
        )
        assert "target file missing" in log.read_text()

    @patch(
        "bluei.engine.lifecycle.classify_finding",
        return_value=RefactorClass.REFACTOR_CLASS,
    )
    @patch(
        "bluei.engine.lifecycle.can_auto_refactor", return_value=(False, "too large")
    )
    @patch("bluei.engine.lifecycle.route_to_human_review", return_value="work-1")
    def test_refactor_safety_gate_routes_to_human(
        self, mock_route, mock_auto, mock_classify, tmp_path, make_finding
    ):
        src = tmp_path / "src"
        src.mkdir()
        (src / "big.ts").write_text("x = 1")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert (
            apply_autofix(
                tmp_path, make_finding(path="src/big.ts", rule="xo-max-lines"), log
            )
            is False
        )
        assert "SAFETY_GATE" in log.read_text()

    @patch(
        "bluei.engine.lifecycle.classify_finding", return_value=RefactorClass.SIMPLE_FIX
    )
    @patch("bluei.engine.lifecycle._recipe_engine")
    def test_perf_pop_front_loop_applies_deque(
        self, mock_re, mock_classify, tmp_path, make_finding
    ):
        mock_re.match.return_value = None
        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text(
            "from os import path\n\ndata = [1, 2, 3]\nwhile data:\n    x = data.pop(0)\n"
        )
        log = tmp_path / "log.txt"
        log.write_text("")
        with (
            patch("bluei.engine.lifecycle._tier_validate", return_value=True),
            patch(
                "bluei.engine.lifecycle.resolve_tier",
                return_value=FixTier.T0_GUARANTEED,
            ),
            patch("bluei.engine.lifecycle._extract_fix_pattern"),
        ):
            apply_autofix(
                tmp_path,
                make_finding(path="src/app.py", rule="perf-pop-front-loop"),
                log,
            )
        content = (src / "app.py").read_text()
        assert "deque" in content
        assert "popleft()" in content


# --- TestApplyCascadeFix ---


class TestApplyCascadeFix:
    """Cascade delegates to deterministic stages, falls to LLM on failure."""

    @patch("bluei.engine.lifecycle._tier_validate", return_value=True)
    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=False)
    @patch("bluei.engine.lifecycle._extract_fix_pattern")
    def test_deterministic_success(
        self,
        mock_ext,
        mock_share,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        mock_tier,
        tmp_path,
        make_finding,
    ):
        mock_ctx_cls.return_value = MagicMock(allow_llm=False)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=True, stage_name="recipe")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        assert apply_cascade_fix(tmp_path, make_finding(), log) is True

    @patch(
        "bluei.engine.lifecycle.apply_claude_fix", return_value=(0, "fixed", "/tmp/p")
    )
    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    @patch("bluei.engine.lifecycle._get_or_create_store")
    @patch("bluei.engine.lifecycle._get_or_create_shared_library")
    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=True)
    def test_falls_to_llm_on_failure(
        self,
        mock_share,
        mock_lib,
        mock_store,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        mock_claude,
        tmp_path,
        make_finding,
    ):
        mock_ctx_cls.return_value = MagicMock(allow_llm=True)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=False, stage_name="recipe")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        assert apply_cascade_fix(tmp_path, make_finding(), log) is True
        mock_claude.assert_called_once()

    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=False)
    def test_deterministic_only_returns_false(
        self,
        mock_share,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        tmp_path,
        make_finding,
    ):
        mock_ctx_cls.return_value = MagicMock(allow_llm=False)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=False, stage_name="recipe")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        assert (
            apply_cascade_fix(tmp_path, make_finding(), log, deterministic_only=True)
            is False
        )

    @pytest.mark.parametrize(
        "path,expected_lang",
        [
            ("src/app.tsx", "typescript"),
            ("src/main.go", "go"),
            ("src/main.rs", "rust"),
            ("src/app.py", "python"),
        ],
    )
    @patch("bluei.engine.cascade.DeterministicCascade")
    @patch("bluei.engine.cascade.CascadeContext")
    @patch("bluei.engine.cascade.default_cascade_stages", return_value=[])
    @patch("bluei.engine.lifecycle._is_pattern_sharing_enabled", return_value=False)
    def test_language_detection(
        self,
        mock_share,
        mock_stages,
        mock_ctx_cls,
        mock_cascade_cls,
        tmp_path,
        make_finding,
        path,
        expected_lang,
    ):
        mock_ctx_cls.return_value = MagicMock(allow_llm=False)
        cascade = MagicMock()
        cascade.execute.return_value = MagicMock(success=False, stage_name="x")
        mock_cascade_cls.return_value = cascade
        log = tmp_path / "log.txt"
        log.write_text("")
        apply_cascade_fix(tmp_path, make_finding(path=path), log)
        assert mock_ctx_cls.call_args[1]["language"] == expected_lang


# --- TestGitOperations ---


class TestGitCommitAll:
    @patch("bluei.engine.git_ops.run_capture")
    def test_commit_succeeds(self, mock_rc, tmp_path):
        mock_rc.side_effect = [(0, ""), (1, "diff"), (0, "[main abc] done")]
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_commit_all(tmp_path, "fix", log, dry_run=False) == "committed"

    @patch("bluei.engine.git_ops.run_capture")
    def test_no_changes(self, mock_rc, tmp_path):
        mock_rc.side_effect = [(0, ""), (0, "")]
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_commit_all(tmp_path, "fix", log, dry_run=False) == "no_changes"

    @patch("bluei.engine.git_ops.run_capture")
    def test_error_on_add_failure(self, mock_rc, tmp_path):
        mock_rc.return_value = (1, "error")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_commit_all(tmp_path, "fix", log, dry_run=False) == "error"

    @patch("bluei.engine.git_ops.run_capture")
    def test_dry_run_skips_commit(self, mock_rc, tmp_path):
        mock_rc.side_effect = [(0, ""), (1, "diff")]
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_commit_all(tmp_path, "fix", log, dry_run=True) == "committed"
        assert "would git commit" in log.read_text()


class TestGitPushBranch:
    @patch("bluei.engine.git_ops.run_capture")
    def test_success(self, mock_rc, tmp_path):
        mock_rc.return_value = (0, "pushed")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_push_branch(tmp_path, "main", log, dry_run=False) is True

    @patch("bluei.engine.git_ops.run_capture")
    def test_failure(self, mock_rc, tmp_path):
        mock_rc.return_value = (1, "push failed")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_push_branch(tmp_path, "main", log, dry_run=False) is False

    def test_dry_run_returns_true(self, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        assert git_push_branch(tmp_path, "main", log, dry_run=True) is True
        assert "would git push" in log.read_text()


class TestDiffStats:
    @patch("bluei.engine.git_ops.run_capture")
    def test_parses_numstat(self, mock_rc, tmp_path):
        mock_rc.return_value = (0, "10\t5\tfile1.py\n3\t2\tfile2.py\n")
        assert diff_stats(tmp_path) == (2, 20)

    @patch("bluei.engine.git_ops.run_capture")
    def test_binary_and_malformed_handled(self, mock_rc, tmp_path):
        mock_rc.return_value = (0, "-\t-\timage.png\nbadline\n5\t3\tfile.py\n")
        files, loc = diff_stats(tmp_path)
        assert files == 2  # binary + valid (malformed skipped)
        assert loc == 8

    @patch("bluei.engine.git_ops.run_capture")
    def test_returns_zero_on_error(self, mock_rc, tmp_path):
        mock_rc.return_value = (1, "error")
        assert diff_stats(tmp_path) == (0, 0)


# --- TestValidation ---


class TestRunValidationGate:
    def test_none_checks_passes(self, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        assert (
            run_validation_gate(tmp_path, tmp_path, None, log_file=log)["passed"]
            is True
        )

    @patch("bluei.engine.validation.run_named_checks")
    def test_regression_detected(self, mock_rnc, tmp_path):
        mock_rnc.side_effect = [
            {"lint": {"rc": 0, "fingerprint": ""}},
            {"lint": {"rc": 1, "fingerprint": "abc"}},
            {"lint": {"rc": 1, "fingerprint": "def"}},
        ]
        log = tmp_path / "log.txt"
        log.write_text("")
        result = run_validation_gate(
            tmp_path, tmp_path, {"lint": ["ruff"]}, log_file=log
        )
        assert result["passed"] is False
        assert "lint" in result["regressions"]

    @patch("bluei.engine.validation.run_named_checks")
    def test_target_failure_blocks(self, mock_rnc, tmp_path):
        mock_rnc.return_value = {"target": {"rc": 1, "fingerprint": "abc"}}
        log = tmp_path / "log.txt"
        log.write_text("")
        result = run_validation_gate(
            tmp_path,
            tmp_path,
            {"target": ["pytest"]},
            baseline_results={"target": {"rc": 1, "fingerprint": "x"}},
            post_fix_results={"target": {"rc": 1, "fingerprint": "y"}},
            log_file=log,
        )
        assert result["passed"] is False
        assert "target" in result["target_failures"]

    def test_pre_computed_results_reused(self, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        baseline = post = target = {"lint": {"rc": 0, "fingerprint": ""}}
        result = run_validation_gate(
            tmp_path,
            tmp_path,
            {"lint": ["ruff"]},
            baseline_results=baseline,
            post_fix_results=post,
            target_results=target,
            log_file=log,
        )
        assert result["passed"] is True
        assert result["baseline_results"] is baseline


class TestRunNamedChecks:
    @patch("bluei.engine.validation.run_capture")
    def test_results_have_rc_and_fingerprint(self, mock_rc, tmp_path):
        mock_rc.return_value = (1, "error output")
        log = tmp_path / "log.txt"
        log.write_text("")
        result = run_named_checks(tmp_path, {"lint": ["ruff", "check"]}, log, "target")
        assert result["lint"]["rc"] == 1
        assert result["lint"]["fingerprint"] != ""

    @patch("bluei.engine.validation.run_capture")
    def test_output_logged(self, mock_rc, tmp_path):
        mock_rc.return_value = (0, "some output text")
        log = tmp_path / "log.txt"
        log.write_text("")
        run_named_checks(tmp_path, {"test": ["pytest"]}, log, "phase1")
        assert "some output text" in log.read_text()


class TestChooseValidationBaseline:
    def test_prefers_worktree(self, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        worktree = {"lint": {"rc": 0, "fingerprint": "wt"}}
        repo = {"lint": {"rc": 0, "fingerprint": "repo"}}
        assert choose_validation_baseline(repo, worktree, log) is worktree

    def test_falls_back_to_repo(self, tmp_path):
        log = tmp_path / "log.txt"
        log.write_text("")
        repo = {"lint": {"rc": 0, "fingerprint": "repo"}}
        assert choose_validation_baseline(repo, {}, log) is repo
        assert "repo-level" in log.read_text()


class TestBuildTargetChecks:
    def test_docs_legacy_reference(self, make_finding):
        assert "target_docs_legacy_reference" in build_target_checks(
            make_finding(rule="docs-legacy-reference", path="docs/api.md")
        )

    def test_test_gap_orders(self, make_finding):
        assert "target_test_gap_orders_case" in build_target_checks(
            make_finding(rule="test-gap-missing-case", path="tests/test_orders.py")
        )

    def test_unknown_rule_empty(self, make_finding):
        assert build_target_checks(make_finding(rule="unknown-rule")) == {}


# --- TestSmokeTest ---


class TestRunSmokeTest:
    @patch("bluei.engine.validation.run_capture")
    def test_clean_repo_passes(self, mock_rc, tmp_path, git_repo, git_commit_all):
        repo = git_repo
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("print('hello')")
        git_commit_all(repo)
        log = tmp_path / "log.txt"
        log.write_text("")

        def side_effect(cmd, cwd=None):
            if cmd[:2] == ["git", "rev-parse"]:
                return (0, ".git")
            if cmd[:2] == ["git", "status"]:
                return (0, "")
            if cmd[0] == "ruff" or (len(cmd) > 1 and cmd[0].endswith("ruff")):
                return (0, "")
            return (0, "")

        mock_rc.side_effect = side_effect
        result = run_smoke_test(repo, log)
        assert result["passed"] is True
        assert result["checks"]["worktree"] is True

    @patch("bluei.engine.validation.run_capture")
    def test_dirty_worktree_fails(self, mock_rc, tmp_path, git_repo, git_commit_all):
        repo = git_repo
        (repo / "dirty.txt").write_text("uncommitted")
        log = tmp_path / "log.txt"
        log.write_text("")

        def side_effect(cmd, cwd=None):
            if cmd[:2] == ["git", "rev-parse"]:
                return (0, ".git")
            if cmd[:2] == ["git", "status"]:
                return (0, "M dirty.txt\n")
            return (0, "")

        mock_rc.side_effect = side_effect
        assert run_smoke_test(repo, log)["checks"]["worktree"] is False

    @patch("bluei.engine.validation.run_capture")
    def test_missing_repo_fails(self, mock_rc, tmp_path):
        mock_rc.return_value = (1, "fatal: not a git repository")
        log = tmp_path / "log.txt"
        log.write_text("")
        assert run_smoke_test(tmp_path / "nonexistent", log)["passed"] is False


# --- TestVerifyFixClosed ---


class TestVerifyFixClosed:
    @patch("bluei.engine.orchestrator.discover_findings", return_value=[])
    def test_true_when_gone(self, mock_disc, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.write_text("")
        assert verify_fix_closed(tmp_path, make_finding(), log) is True

    @patch("bluei.engine.orchestrator.discover_findings")
    def test_false_when_persists(self, mock_disc, tmp_path, make_finding):
        mock_disc.return_value = [make_finding()]
        log = tmp_path / "log.txt"
        log.write_text("")
        assert verify_fix_closed(tmp_path, make_finding(), log) is False
        assert "still_firing=1" in log.read_text()


# --- TestRouteToHumanReview ---


class TestRouteToHumanReview:
    @patch("bluei.engine.refactor_queue.enqueue_refactor_work")
    def test_successful_enqueue(self, mock_enqueue, tmp_path, make_finding):
        entry = MagicMock(work_id="work-123")
        mock_enqueue.return_value = entry
        log = tmp_path / "log.txt"
        log.write_text("")
        rw = RefactorWork(finding_id="test-f-001")
        rw.mark_aborted("safety_gate")
        assert route_to_human_review(make_finding(), rw, tmp_path, log) == "work-123"
        assert "enqueued" in log.read_text()

    @patch(
        "bluei.engine.refactor_queue.enqueue_refactor_work",
        side_effect=Exception("broken"),
    )
    def test_failed_enqueue(self, mock_enqueue, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.write_text("")
        rw = RefactorWork(finding_id="test-f-001")
        rw.mark_aborted("safety_gate")
        assert route_to_human_review(make_finding(), rw, tmp_path, log) == ""
        assert "failed to enqueue" in log.read_text()


# --- TestSelfHealing ---


class TestRunStartupSelfHealing:
    @patch("bluei.engine.startup.run_capture", return_value=(0, ""))
    @patch("bluei.engine.startup._parse_worktree_list_porcelain", return_value=[])
    @patch("bluei.engine.startup.repair_state", return_value=False)
    @patch("bluei.engine.startup.load_batches", return_value=[])
    def test_removes_stale_locks(
        self, mock_batches, mock_repair, mock_parse, mock_rc, tmp_path
    ):
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        old_lock = locks_dir / "old.lock"
        old_lock.write_text("lock")
        old_time = time.time() - (STALE_LOCK_HOURS + 1) * 3600
        os.utime(old_lock, (old_time, old_time))
        log = tmp_path / "log.txt"
        log.write_text("")
        with (
            patch("bluei.engine.startup.datetime", datetime, create=True),
            patch("bluei.engine.startup.timezone", timezone, create=True),
        ):
            result = run_startup_self_healing(tmp_path, log, locks_dir=locks_dir)
        assert result["stale_locks_removed"] == 1
        assert not old_lock.exists()

    @patch("bluei.engine.startup.run_capture", return_value=(0, ""))
    @patch("bluei.engine.startup._parse_worktree_list_porcelain", return_value=[])
    @patch("bluei.engine.startup.repair_state", return_value=False)
    @patch("bluei.engine.startup.load_batches", return_value=[])
    def test_preserves_fresh_locks(
        self, mock_batches, mock_repair, mock_parse, mock_rc, tmp_path
    ):
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        fresh_lock = locks_dir / "fresh.lock"
        fresh_lock.write_text("lock")
        log = tmp_path / "log.txt"
        log.write_text("")
        with (
            patch("bluei.engine.startup.datetime", datetime, create=True),
            patch("bluei.engine.startup.timezone", timezone, create=True),
        ):
            result = run_startup_self_healing(tmp_path, log, locks_dir=locks_dir)
        assert result["stale_locks_removed"] == 0
        assert fresh_lock.exists()

    @patch("bluei.engine.startup.run_capture", return_value=(0, ""))
    @patch("bluei.engine.startup._parse_worktree_list_porcelain", return_value=[])
    @patch("bluei.engine.startup.repair_state", return_value=False)
    @patch("bluei.engine.startup.load_batches", return_value=[])
    def test_dry_run_preserves_files(
        self, mock_batches, mock_repair, mock_parse, mock_rc, tmp_path
    ):
        locks_dir = tmp_path / "locks"
        locks_dir.mkdir()
        old_lock = locks_dir / "old.lock"
        old_lock.write_text("lock")
        old_time = time.time() - (STALE_LOCK_HOURS + 1) * 3600
        os.utime(old_lock, (old_time, old_time))
        with (
            patch("bluei.engine.startup.datetime", datetime, create=True),
            patch("bluei.engine.startup.timezone", timezone, create=True),
        ):
            result = run_startup_self_healing(
                tmp_path, locks_dir=locks_dir, dry_run=True
            )
        assert result["stale_locks_removed"] == 1
        assert old_lock.exists()

    @patch("bluei.engine.startup.run_capture", return_value=(1, "error"))
    @patch("bluei.engine.startup._parse_worktree_list_porcelain", return_value=[])
    @patch("bluei.engine.startup.repair_state", return_value=False)
    @patch("bluei.engine.startup.load_batches", return_value=[])
    def test_worktree_prune_failure_logged(
        self, mock_batches, mock_repair, mock_parse, mock_rc, tmp_path
    ):
        log = tmp_path / "log.txt"
        log.write_text("")
        result = run_startup_self_healing(tmp_path, log)
        assert result["worktrees_pruned"] is False
        assert any("worktree-prune" in e for e in result["errors"])

    @patch("bluei.engine.startup.run_capture", return_value=(0, ""))
    @patch("bluei.engine.startup._parse_worktree_list_porcelain", return_value=[])
    @patch("bluei.engine.startup.repair_state", return_value=True)
    @patch("bluei.engine.startup.load_batches", return_value=[])
    @patch("bluei.engine.constants.DEFAULT_STATE")
    def test_state_repair_triggered(
        self, mock_ds, mock_batches, mock_repair, mock_parse, mock_rc, tmp_path
    ):
        mock_ds.exists.return_value = True
        log = tmp_path / "log.txt"
        log.write_text("")
        run_startup_self_healing(tmp_path, log)
        mock_repair.assert_called()


# --- TestProcessRefactorQueue ---


class TestProcessRefactorQueue:
    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_dry_run_lists_items(self, mock_rq_cls, tmp_path):
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        mock_queue.list_items.return_value = [_make_queue_item("w-1")]
        result = process_refactor_queue(tmp_path, tmp_path, dry_run=True)
        assert "w-1" in result["processed"]

    @patch(
        "bluei.engine.lifecycle.apply_claude_fix", return_value=(0, "fixed", "/tmp/p")
    )
    @patch("bluei.engine.validation.run_validation_gate", return_value={"passed": True})
    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_success_completes_item(self, mock_rq_cls, mock_vg, mock_claude, tmp_path):
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        item = _make_queue_item("w-2")
        mock_queue.list_items.side_effect = [[item], []]
        result = process_refactor_queue(tmp_path, tmp_path, dry_run=False)
        assert "w-2" in result["processed"]
        mock_queue.complete.assert_called_once_with("w-2")

    @patch(
        "bluei.engine.lifecycle.apply_claude_fix", return_value=(1, "error", "/tmp/p")
    )
    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_claude_failure_marks_failed(self, mock_rq_cls, mock_claude, tmp_path):
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        item = _make_queue_item("w-3")
        mock_queue.list_items.side_effect = [[item], []]
        result = process_refactor_queue(tmp_path, tmp_path, dry_run=False)
        assert "w-3" in result["failed"]
        mock_queue.fail.assert_called_once()

    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_auto_approve_moves_pending(self, mock_rq_cls, tmp_path):
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        mock_pending = MagicMock(work_id="w-pend")
        mock_queue.list_items.side_effect = [[mock_pending], [], []]
        result = process_refactor_queue(
            tmp_path, tmp_path, dry_run=False, auto_approve=True
        )
        mock_queue.approve.assert_called_once_with("w-pend", "auto_approved")
        assert "w-pend" in result["approved"]

    @patch("bluei.engine.lifecycle.RefactorQueue")
    def test_max_items_limits_processing(self, mock_rq_cls, tmp_path):
        mock_queue = MagicMock()
        mock_rq_cls.return_value = mock_queue
        mock_queue.list_items.return_value = [
            _make_queue_item(f"w-{i}") for i in range(5)
        ]
        result = process_refactor_queue(tmp_path, tmp_path, dry_run=True, max_items=2)
        assert len(result["processed"]) == 2


# --- TestClassifyReviewFeedback ---


class TestClassifyReviewFeedback:
    @pytest.mark.parametrize(
        "text",
        [
            "Please reconsider the architecture",
            "This requires a design change",
            "Product decision needed here",
            "This is a conceptual issue",
            "We need to rethink this",
        ],
    )
    def test_needs_human(self, text):
        assert classify_review_feedback(text) == "needs-human"

    @pytest.mark.parametrize(
        "text",
        [
            "Use a more descriptive variable name",
            "Fix typo in docstring",
            "Add missing return type annotation",
        ],
    )
    def test_actionable(self, text):
        assert classify_review_feedback(text) == "actionable"


# --- TestReviewLoopAllowed ---


class TestReviewLoopAllowed:
    @pytest.mark.parametrize(
        "author,tags,expected",
        [
            ("bluei-bot", [], True),
            ("human", ["auto-review"], True),
        ],
    )
    def test_allowed(self, author, tags, expected):
        ok, _ = review_loop_allowed(author, tags, "bluei-bot", "auto-review")
        assert ok is expected

    @pytest.mark.parametrize(
        "author,tags",
        [
            ("human", []),
            ("human", ["other-tag"]),
        ],
    )
    def test_blocked(self, author, tags):
        ok, reason = review_loop_allowed(author, tags, "bluei-bot", "auto-review")
        assert ok is False


class TestLifecycleCacheReset:
    """Pin _reset_lifecycle_cache behavior."""

    def test_reset_clears_mnemo_clients(self):
        from bluei.engine.lifecycle import _mnemo_clients, _reset_lifecycle_cache

        _mnemo_clients["test_key"] = MagicMock()
        _reset_lifecycle_cache()
        assert len(_mnemo_clients) == 0

    def test_reset_clears_cached_stores(self):
        from bluei.engine.lifecycle import _cached_stores, _reset_lifecycle_cache

        _cached_stores["test_key"] = object()
        _reset_lifecycle_cache()
        assert len(_cached_stores) == 0

    def test_get_recipe_engine_constructs_once(self):
        from bluei.engine.lifecycle import (
            _get_recipe_engine,
            _reset_lifecycle_cache,
        )

        _reset_lifecycle_cache()
        engine1 = _get_recipe_engine()
        engine2 = _get_recipe_engine()
        assert engine1 is engine2
        _reset_lifecycle_cache()

    def test_get_tiered_validator_constructs_once(self):
        from bluei.engine.lifecycle import (
            _get_tiered_validator,
            _reset_lifecycle_cache,
        )

        _reset_lifecycle_cache()
        v1 = _get_tiered_validator()
        v2 = _get_tiered_validator()
        assert v1 is v2
        _reset_lifecycle_cache()

    def test_reset_allows_reconstruction(self):
        from bluei.engine.lifecycle import (
            _get_recipe_engine,
            _get_tiered_validator,
            _reset_lifecycle_cache,
        )

        _reset_lifecycle_cache()
        engine1 = _get_recipe_engine()
        validator1 = _get_tiered_validator()
        _reset_lifecycle_cache()
        engine2 = _get_recipe_engine()
        validator2 = _get_tiered_validator()
        assert engine1 is not engine2
        assert validator1 is not validator2
