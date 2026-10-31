"""Selected single-finding repair: fail-closed gating in the PR cycle.

Regression tests for repairing exactly one freshly discovered Python finding
with a single model attempt: candidate restriction before unrelated issue
mutations, duplicate/missing/ineligible fail-closed, cascade and governor
skips, accurate invocation counting, scoped final verification, no-learning
freeze, and retention of the local patch artifact.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.pr_cycle import run_pr_cycle_phase
from bluei.engine.models import Finding

SELECTED_ID = "py-f821-0001"


def _fake_git_output(command, **kwargs):
    if command[1] == "diff":
        kwargs["stdout"].write(b"diff --git a/src/billing.py b/src/billing.py\n")
    return SimpleNamespace(returncode=0, stdout=b"")


def _fresh_finding(**overrides):
    defaults = dict(
        finding_id=SELECTED_ID,
        repo="acme/widget",
        path="src/billing.py",
        line=7,
        rule="ruff-F821",
        snippet="totl_price = price * qty",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
        fix_attempts=0,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _selected_args(tmp_path, **overrides):
    """Mirror the parser-forced namespace for a selected model-lane run."""
    defaults = dict(
        repo_path=str(tmp_path),
        run_phase="orchestrated",
        discovery_scope="python",
        finding_id=SELECTED_ID,
        no_learning=True,
        fix_engine="claude",
        live_github_actions=False,
        dry_run=False,
        allow_main_commit=False,
        force_push=False,
        max_prs_per_run=1,
        max_issues_per_run=1,
        max_fix_attempts_per_issue=1,
        open_prs_cap=5,
        issue_confidence_threshold=0.5,
        batch_pr_enabled=False,
        deterministic_only=False,
        max_files_changed=5,
        max_loc_diff=200,
        allow_unchanged_baseline_failures=True,
        claude_cmd_template="fake-model {prompt_file}",
        pattern_store_path=None,
        batch_state_file=str(tmp_path / "batches.jsonl"),
        workspace=str(tmp_path),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _issue(finding_id=SELECTED_ID, **overrides):
    defaults = dict(
        issue_id=f"issue-{finding_id}",
        finding_id=finding_id,
        status="open",
        rule="ruff-F821",
        path="src/billing.py",
        # Stale on purpose: the repair must use the fresh finding's line.
        line=99,
        snippet="totl_price = price * qty",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
    )
    defaults.update(overrides)
    return defaults


def _ctx(tmp_path, args, issues, eligible=None):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    fresh = [] if eligible is None else eligible
    return RunContext(
        args=args,
        repo_path=tmp_path / "repo",
        state_file=tmp_path / "state" / "run.json",
        findings_file=tmp_path / "findings.jsonl",
        log_file=tmp_path / "run.log",
        worktree_root=tmp_path / "worktrees",
        gh_repo_slug="acme/widget",
        state={},
        issues_data={"issues": issues},
        eligible_findings=fresh,
        findings=fresh,
        detector_outcomes={"plugin:plugin-python": {"status": "ran", "count": len(fresh)}},
        PER_REPO_BASELINE_CHECKS={},
        cost_tracker=MagicMock(exceeded_limit=Mock(return_value=False)),
        pattern_store=None,
        # Direct-API callers may arrive unpaused; the cycle must force it.
        learning_mode="active",
        safety_config={"mode": "observe"},
    )


class TestSelectedFailClosed:
    def test_missing_fresh_finding_fails_closed(self, tmp_path):
        unrelated = _issue(finding_id="other-1", issue_id="i-other")
        ctx = _ctx(tmp_path, _selected_args(tmp_path), [unrelated])
        with patch("bluei.engine.commands.pr_cycle.get_branch", return_value="x"), patch(
            "bluei.engine.commands.pr_cycle.apply_claude_fix"
        ) as fix:
            result = run_pr_cycle_phase(ctx)
        assert "not a single fresh eligible" in result["selected_error"]
        assert result["selected_error"] in result["blocked_reasons"]
        assert result["fix_attempts"] == 0
        assert result["fixes_verified"] == 0
        fix.assert_not_called()
        assert unrelated["status"] == "open"

    @pytest.mark.parametrize("status", ["failed", "unavailable", "skipped"])
    def test_incomplete_selected_producer_leaves_all_issues_untouched(self, tmp_path, status):
        issues = [_issue(), _issue(finding_id="other-1", rule="not-model-fixable")]
        before = [dict(issue) for issue in issues]
        ctx = _ctx(tmp_path, _selected_args(tmp_path), issues, eligible=[_fresh_finding()])
        ctx.detector_outcomes = {"plugin:plugin-python": {"status": status}}
        with patch("bluei.engine.commands.pr_cycle.apply_claude_fix") as fix:
            result = run_pr_cycle_phase(ctx)
        assert result["selected_error"] and result["fix_attempts"] == 0
        assert issues == before
        fix.assert_not_called()

    def test_suppressed_finding_does_not_fall_back_to_raw_discovery(self, tmp_path):
        issues = [_issue()]
        ctx = _ctx(tmp_path, _selected_args(tmp_path), issues)
        ctx.findings = [_fresh_finding()]
        with patch("bluei.engine.commands.pr_cycle.apply_claude_fix") as fix:
            result = run_pr_cycle_phase(ctx)
        assert result["selected_error"] and issues[0]["status"] == "open"
        fix.assert_not_called()

    def test_duplicate_issue_records_fail_closed_untouched(self, tmp_path):
        dupes = [_issue(), _issue(issue_id="i-dup-2")]
        ctx = _ctx(tmp_path, _selected_args(tmp_path), dupes, eligible=[_fresh_finding()])
        with patch("bluei.engine.commands.pr_cycle.get_branch", return_value="x"), patch(
            "bluei.engine.commands.pr_cycle.apply_claude_fix"
        ) as fix:
            result = run_pr_cycle_phase(ctx)
        assert "exactly one issue" in result["selected_error"]
        assert result["fix_attempts"] == 0
        fix.assert_not_called()
        assert all(issue["status"] == "open" for issue in dupes)

    def test_selected_dry_run_can_validate_without_creating_an_issue(self, tmp_path):
        ctx = _ctx(tmp_path, _selected_args(tmp_path, dry_run=True), [], eligible=[_fresh_finding()])
        with patch("bluei.engine.commands.pr_cycle.apply_claude_fix") as model:
            result = run_pr_cycle_phase(ctx)
        assert not result["selected_error"] and result["fix_attempts"] == 0
        assert result["issues_data"] == {"issues": []}
        model.assert_not_called()

    def test_selected_dry_run_does_not_invoke_model_or_create_worktree(self, tmp_path):
        issue = _issue()
        before = dict(issue)
        ctx = _ctx(tmp_path, _selected_args(tmp_path, dry_run=True), [issue], eligible=[_fresh_finding()])
        with patch("bluei.engine.commands.pr_cycle.apply_claude_fix") as fix, patch(
            "bluei.engine.commands.pr_cycle.create_worktree"
        ) as worktree:
            result = run_pr_cycle_phase(ctx)
        assert not result["selected_error"] and result["fix_attempts"] == 0
        fix.assert_not_called()
        worktree.assert_not_called()
        assert issue == before

    def test_non_model_engine_direct_api_fails_closed(self, tmp_path):
        issues = [_issue()]
        ctx = _ctx(
            tmp_path,
            _selected_args(tmp_path, fix_engine="deterministic"),
            issues,
            eligible=[_fresh_finding()],
        )
        with patch("bluei.engine.commands.pr_cycle.get_branch", return_value="x"), patch(
            "bluei.engine.commands.pr_cycle.apply_claude_fix"
        ) as fix:
            result = run_pr_cycle_phase(ctx)
        assert "not a single fresh eligible" in result["selected_error"]
        fix.assert_not_called()
        assert issues[0]["status"] == "open"

    def test_non_llm_fixable_rule_fails_closed(self, tmp_path):
        issues = [_issue(rule="ruff-F999")]
        ctx = _ctx(
            tmp_path,
            _selected_args(tmp_path),
            issues,
            eligible=[_fresh_finding(rule="ruff-F999")],
        )
        with patch("bluei.engine.commands.pr_cycle.get_branch", return_value="x"), patch(
            "bluei.engine.commands.pr_cycle.apply_claude_fix"
        ) as fix, patch(
            "bluei.engine.commands.pr_cycle._get_llm_fixable_rules",
            return_value={},
        ):
            result = run_pr_cycle_phase(ctx)
        assert "not eligible" in result["selected_error"]
        assert result["fix_attempts"] == 0
        fix.assert_not_called()

    @pytest.mark.parametrize("engine", ["claude", "opencode"])
    def test_failed_model_attempt_is_public_failure(self, tmp_path, engine):
        issues = [_issue()]
        ctx = _ctx(
            tmp_path,
            _selected_args(tmp_path, fix_engine=engine),
            issues,
            eligible=[_fresh_finding()],
        )
        with patch("bluei.engine.commands.pr_cycle.get_branch", return_value="x"), patch(
            "bluei.engine.commands.pr_cycle.apply_claude_fix",
            return_value=(1, "model exploded", "prompt.txt"),
        ) as fix, patch(
            "bluei.engine.commands.pr_cycle.create_worktree",
            return_value=SimpleNamespace(success=True),
        ), patch("bluei.engine.commands.pr_cycle.hydrate_worktree"), patch(
            "bluei.engine.commands.pr_cycle.remove_worktree"
        ), patch(
            "bluei.engine.commands.pr_cycle.run_named_checks", return_value={}
        ):
            result = run_pr_cycle_phase(ctx)
        assert fix.call_count == 1
        assert result["claude_invocations"] == 1
        assert result["fix_attempts"] == 1
        assert result["fixes_failed_verification"] == 1
        assert result["fixes_verified"] == 0
        assert result["created_prs"] == 0
        assert "failed its single model attempt" in result["selected_error"]
        assert result["selected_error"] in result["blocked_reasons"]


class TestSelectedSingleModelAttempt:
    def _run_success(self, tmp_path, engine="claude"):
        issues = [_issue(), _issue(finding_id="other-9", issue_id="i-unrel")]
        ctx = _ctx(
            tmp_path,
            _selected_args(tmp_path, fix_engine=engine),
            issues,
            eligible=[_fresh_finding()],
        )
        patches = [
            patch("bluei.engine.commands.pr_cycle.get_branch", return_value="feature"),
            patch(
                "bluei.engine.commands.pr_cycle.apply_claude_fix",
                return_value=(0, "fixed", "prompt.txt"),
            ),
            patch(
                "bluei.engine.commands.pr_cycle.create_worktree",
                return_value=SimpleNamespace(success=True),
            ),
            patch("bluei.engine.commands.pr_cycle.hydrate_worktree"),
            patch("bluei.engine.commands.pr_cycle.remove_worktree"),
            patch("bluei.engine.commands.pr_cycle.run_named_checks", return_value={}),
            patch(
                "bluei.engine.commands.pr_cycle.run_validation_gate",
                return_value={"passed": True, "message": "ok"},
            ),
            patch(
                "bluei.engine.commands.pr_cycle.diff_stats",
                return_value=(1, 4),
            ),
            patch(
                "bluei.engine.commands.pr_cycle.verify_fix_closed",
                return_value=SimpleNamespace(is_closed=True),
            ),
            patch(
                "bluei.engine.commands.pr_cycle.load_authoritative_guidelines",
                return_value=None,
            ),
            patch(
                "bluei.engine.commands.pr_cycle.load_repo_taste", return_value=None
            ),
            patch(
                "bluei.engine.commands.pr_cycle.subprocess.run",
                side_effect=_fake_git_output,
            ),
            patch(
                "bluei.engine.model_governor.resolve_governed_model",
                new=MagicMock(),
            ),
            patch("bluei.engine.lifecycle.apply_cascade_fix", new=MagicMock()),
            patch("bluei.engine.sprt.run_sprt_check", new=MagicMock()),
        ]
        mocks = [p.start() for p in patches]
        try:
            result = run_pr_cycle_phase(ctx)
        finally:
            for p in patches:
                p.stop()
        fix = mocks[1]
        verify = mocks[8]
        sub_run = mocks[11]
        governor, cascade, sprt = mocks[12], mocks[13], mocks[14]
        return ctx, result, issues, fix, verify, sub_run, governor, cascade, sprt

    def test_selected_f821_single_model_attempt(self, tmp_path):
        (
            ctx,
            result,
            issues,
            fix,
            verify,
            sub_run,
            governor,
            cascade,
            sprt,
        ) = self._run_success(tmp_path)

        # One actual model call, no cascade, no governor, no SPRT (paused).
        assert fix.call_count == 1
        cascade.assert_not_called()
        governor.assert_not_called()
        sprt.assert_not_called()
        assert ctx.learning_mode == "paused"

        req = fix.call_args[0][0]
        # The fresh finding drives the attempt, not the stale issue copy.
        assert req.finding.line == 7
        assert req.finding.finding_id == SELECTED_ID
        # Operator template verbatim; no lesson reads/writes under no-learning.
        assert req.claude_cmd_template == "fake-model {prompt_file}"
        assert req.lessons_file is None
        # The F821 prompt hint from llm_fixable_rules.yaml reaches the model.
        assert "F821" in (req.extra_prompt or "")

        # Scoped final verification threads the discovery scope through.
        assert verify.call_count == 1
        assert verify.call_args.kwargs.get("discovery_scope") == "python"

        # No-live: nothing published, but the repair is verified and the diff
        # artifact is retained before the worktree cleanup.
        assert result["selected_error"] == ""
        assert result["created_prs"] == 0
        assert result["fixes_verified"] == 1
        assert result["claude_invocations"] == 1
        patch_path = Path(issues[0]["selected_fix_patch"])
        assert patch_path.exists()
        assert patch_path.read_bytes().startswith(b"diff --git")
        assert issues[0]["selected_fix_patch"] == str(patch_path)
        assert issues[0]["status"] == "open"
        assert sub_run.call_args.args[0] == ["git", "diff", "--binary", "HEAD"]
        assert sub_run.call_args.kwargs["cwd"].startswith(str(ctx.worktree_root))

        # Only the selected issue was touched; the unrelated one is intact.
        assert issues[1]["status"] == "open"
        assert "selected_fix_patch" not in issues[1]

    def test_patch_artifacts_do_not_overwrite_previous_attempt(self, tmp_path):
        first = self._run_success(tmp_path)
        first_path = Path(first[2][0]["selected_fix_patch"])
        original = first_path.read_bytes()
        second = self._run_success(tmp_path)
        second_path = Path(second[2][0]["selected_fix_patch"])
        assert first_path != second_path and second_path.exists()
        assert first_path.read_bytes() == original
        assert second[1]["fixes_verified"] == 1

    def test_selected_opencode_entrypoint_reaches_model_lane(self, tmp_path):
        ctx, result, issues, fix, *_ = self._run_success(tmp_path, engine="opencode")
        assert fix.call_count == 1
        assert result["selected_error"] == ""
        assert result["claude_invocations"] == 1
        assert result["fixes_verified"] == 1
