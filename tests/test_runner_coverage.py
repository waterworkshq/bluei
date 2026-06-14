"""Tests for bluei/app/runner.py — backend resolution, lock handling, output parsing."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bluei.app.config import ConfigManager
from bluei.app.health import HealthEngine
from bluei.app.models import Repo, RepoConfig, RepoStatus, Run
from bluei.app.registry import RepoRegistry
from bluei.app.runner import RunEngine, RunOptions
from bluei.app.state import StateManager


@pytest.fixture
def runner_env(tmp_path):
    workspace = tmp_path / "qa-agent"
    (workspace / "repos").mkdir(parents=True)
    (workspace / "plugins").mkdir()
    (workspace / "logs").mkdir()

    config = ConfigManager(workspace)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    runner = RunEngine(registry, state, health, config)
    return {
        "workspace": workspace,
        "config": config,
        "registry": registry,
        "health": health,
        "state": state,
        "runner": runner,
    }


@pytest.fixture
def test_repo(runner_env):
    repo_config = RepoConfig(
        id="test-001",
        name="test-repo",
        path="/tmp/test-repo",
        language="python",
    )
    return runner_env["registry"].create(repo_config)


class TestBackendAvailable:
    def test_deterministic_always_available(self, runner_env):
        assert runner_env["runner"]._backend_available("deterministic") is True

    @patch("bluei.app.runner.shutil.which", return_value="/usr/bin/claude")
    def test_claude_available_when_installed(self, mock_which, runner_env):
        assert runner_env["runner"]._backend_available("claude") is True

    @patch("bluei.app.runner.shutil.which", return_value=None)
    def test_claude_not_available(self, mock_which, runner_env):
        assert runner_env["runner"]._backend_available("claude") is False

    @patch("bluei.app.runner.shutil.which", return_value="/usr/bin/opencode")
    def test_opencode_available(self, mock_which, runner_env):
        assert runner_env["runner"]._backend_available("opencode") is True

    def test_unknown_backend_not_available(self, runner_env):
        assert runner_env["runner"]._backend_available("unknown") is False


class TestTemplateForBackend:
    def test_claude_default_template(self, runner_env, test_repo):
        test_repo.config.claude_template = None
        result = runner_env["runner"]._template_for_backend(test_repo, "claude")
        assert "claude" in result
        assert "{prompt_file}" in result

    def test_claude_custom_template(self, runner_env, test_repo):
        test_repo.config.claude_template = "custom claude {finding_id}"
        result = runner_env["runner"]._template_for_backend(test_repo, "claude")
        assert result == "custom claude {finding_id}"

    def test_opencode_default_template(self, runner_env, test_repo):
        test_repo.config.opencode_template = None
        result = runner_env["runner"]._template_for_backend(test_repo, "opencode")
        assert "opencode" in result

    def test_opencode_custom_template(self, runner_env, test_repo):
        test_repo.config.opencode_template = "custom opencode {finding_id}"
        result = runner_env["runner"]._template_for_backend(test_repo, "opencode")
        assert result == "custom opencode {finding_id}"

    def test_unknown_backend_returns_none(self, runner_env, test_repo):
        assert runner_env["runner"]._template_for_backend(test_repo, "unknown") is None


class TestResolveBackend:
    def test_deterministic_requested(self, runner_env, test_repo):
        result = runner_env["runner"]._resolve_backend(test_repo, "deterministic")
        assert result["logical_backend"] == "deterministic"
        assert result["template"] is None

    def test_invalid_requested_falls_back(self, runner_env, test_repo):
        result = runner_env["runner"]._resolve_backend(test_repo, "invalid_backend")
        assert result["logical_backend"] is not None

    @patch("bluei.app.runner.shutil.which", return_value=None)
    def test_auto_falls_to_deterministic(self, mock_which, runner_env, test_repo):
        result = runner_env["runner"]._resolve_backend(test_repo, "auto")
        assert result["logical_backend"] == "deterministic"

    @patch("bluei.app.runner.shutil.which", return_value="/usr/bin/claude")
    def test_auto_prefers_available_backend(self, mock_which, runner_env, test_repo):
        result = runner_env["runner"]._resolve_backend(test_repo, "auto")
        assert result["logical_backend"] == "claude"

    def test_no_requested_uses_config_default(self, runner_env, test_repo):
        result = runner_env["runner"]._resolve_backend(test_repo, None)
        assert result["logical_backend"] is not None


class TestLocking:
    def test_acquire_and_release_lock(self, runner_env):
        handle = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert handle is not None
        runner_env["runner"]._release_lock(handle)

    def test_lock_contention(self, runner_env):
        handle1 = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        handle2 = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert handle1 is not None
        assert handle2 is None
        runner_env["runner"]._release_lock(handle1)

    def test_different_phases_different_locks(self, runner_env):
        h1 = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        h2 = runner_env["runner"]._acquire_lock("test-repo", "pr-cycle")
        assert h1 is not None
        assert h2 is not None
        runner_env["runner"]._release_lock(h1)
        runner_env["runner"]._release_lock(h2)

    def test_review_merge_share_lock(self, runner_env):
        h1 = runner_env["runner"]._acquire_lock("test-repo", "review-cycle")
        h2 = runner_env["runner"]._acquire_lock("test-repo", "merge-cycle")
        assert h1 is not None
        assert h2 is None
        runner_env["runner"]._release_lock(h1)

    def test_release_none_handle(self, runner_env):
        runner_env["runner"]._release_lock(None)

    def test_lock_path_review_merge_shared(self, runner_env):
        path1 = runner_env["runner"]._lock_path("repo", "review-cycle")
        path2 = runner_env["runner"]._lock_path("repo", "merge-cycle")
        assert path1 == path2

    def test_lock_path_different_phases(self, runner_env):
        path1 = runner_env["runner"]._lock_path("repo", "issue-cycle")
        path2 = runner_env["runner"]._lock_path("repo", "pr-cycle")
        assert path1 != path2


class TestOutputParsing:
    def test_parse_merges(self, runner_env):
        output = "merges=3 merged=2"
        metrics = runner_env["runner"]._parse_output(output)
        assert metrics["merges_completed"] > 0

    def test_parse_fix_attempts(self, runner_env):
        output = "fix_attempts=5 attempts=3"
        metrics = runner_env["runner"]._parse_output(output)
        assert metrics["fix_attempts"] == 5

    def test_parse_empty_output(self, runner_env):
        metrics = runner_env["runner"]._parse_output("")
        assert metrics["findings_detected"] == 0
        assert metrics["issues_created"] == 0

    def test_parse_created_pr_pattern(self, runner_env):
        output = "created 3 pr"
        metrics = runner_env["runner"]._parse_output(output)
        assert metrics["prs_created"] == 3


class TestBuildCliArgs:
    def test_includes_status_file(self, runner_env, test_repo):
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--status-file" in args

    def test_includes_docs_index_file(self, runner_env, test_repo):
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--docs-index-file" in args

    def test_includes_no_dry_run(self, runner_env, test_repo):
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=False)
        )
        assert "--no-dry-run" in args

    def test_includes_fix_engine(self, runner_env, test_repo):
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--fix-engine" in args

    @patch("bluei.app.runner.shutil.which", return_value="/usr/bin/claude")
    def test_includes_claude_template_for_claude_backend(
        self, mock_which, runner_env, test_repo
    ):
        opts = RunOptions(phase="issue-cycle", dry_run=True, fix_engine="claude")
        backend = runner_env["runner"]._resolve_backend(test_repo, "claude")
        args = runner_env["runner"]._build_cli_args(test_repo, opts, backend)
        assert "--claude-cmd-template" in args

    def test_live_github_actions_flag(self, runner_env, test_repo):
        test_repo.config.github["live_actions"] = True
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--live-github-actions" in args

    def test_auto_merge_flag(self, runner_env, test_repo):
        test_repo.config.github["auto_merge"] = True
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--auto-merge-sandbox" in args

    def test_auto_rebase_flags(self, runner_env, test_repo):
        test_repo.config.github["auto_rebase"] = {
            "enabled": True,
            "max_prs_per_sweep": 3,
        }
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--auto-rebase-enabled" in args
        assert "--rebase-max-prs" in args

    def test_baseline_checks_flag(self, runner_env, test_repo):
        test_repo.config.baseline_checks = ["pytest -q", "ruff check ."]
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert "--baseline-checks" in args

    def test_auto_tune_overrides(self, runner_env, test_repo):
        tune_path = runner_env["runner"].config.workspace / "state" / "auto_tune.json"
        tune_path.parent.mkdir(parents=True, exist_ok=True)
        tune_path.write_text(
            '{"tuned_fields": {"max_prs_per_run": 1, "finding_cooldown_seconds": 7200}}'
        )
        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        idx = args.index("--max-prs-per-run") + 1
        assert args[idx] == "1"


class TestCleanupStaleArtifacts:
    def test_removes_stale_locks(self, runner_env):
        lock_dir = runner_env["runner"].config.workspace / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        stale_lock = lock_dir / "old.lock"
        stale_lock.write_text("old")
        import os
        import time

        old_time = time.time() - 20000
        os.utime(stale_lock, (old_time, old_time))

        runner_env["runner"]._cleanup_stale_artifacts()
        assert not stale_lock.exists()


class TestDryRunMethod:
    def test_dry_run_method_creates_dry_options(self, runner_env, test_repo):
        with patch.object(runner_env["runner"], "run") as mock_run:
            mock_run.return_value = MagicMock()
            runner_env["runner"].dry_run(test_repo, "issue-cycle")
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][1].dry_run is True
            assert args[0][1].phase == "issue-cycle"


# ---------------------------------------------------------------------------
# Integration-level run() tests — test public behavior through the main entry
# point, mocking only subprocess.run at the module boundary.
# ---------------------------------------------------------------------------

_REALISTIC_OUTPUT = (
    "Running issue-cycle for test-repo\n"
    "findings=4 issues=2 fix_attempts=1 fixes_verified=1 fixes_failed=0\n"
    "prs=1 merges=0\n"
    "DONE\n"
)


def _make_subprocess_ok(stdout: str = _REALISTIC_OUTPUT, stderr: str = ""):
    """Return a mock subprocess.CompletedProcess with rc=0."""
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestRunExecution:
    """Test RunEngine.run() end-to-end with mocked subprocess."""

    def test_successful_run_returns_success(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        assert result.success is True
        assert result.run.status == "completed"
        assert result.run.findings_detected == 4
        assert result.run.issues_created == 2
        assert result.run.prs_created == 1

    def test_successful_run_creates_log_file(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        log_dir = runner_env["config"].workspace / "logs" / "test-repo"
        log_files = list(log_dir.glob("*.log"))
        assert len(log_files) == 1
        assert "findings=4" in log_files[0].read_text()

    def test_successful_run_records_health_trend(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        trend_file = runner_env["config"].workspace / "state" / "health_trend.jsonl"
        assert trend_file.exists()
        lines = trend_file.read_text().strip().split("\n")
        assert len(lines) >= 1
        import json

        record = json.loads(lines[-1])
        assert record["repo"] == "test-repo"
        assert record["findings_detected"] == 4

    def test_successful_run_updates_registry(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        repo = runner_env["registry"].find_by_name("test-repo")
        assert repo.status == RepoStatus.READY

    def test_failed_run_nonzero_rc(self, runner_env, test_repo):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "something went wrong in the engine"
        with patch("bluei.app.runner.subprocess.run", return_value=proc):
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        assert result.success is False
        assert result.run.status == "failed"
        assert result.run.error == "something went wrong in the engine"

    def test_timeout_run(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.side_effect = subprocess.TimeoutExpired(cmd=[], timeout=3600)
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        assert result.success is False
        assert result.run.status == "timeout"
        assert "timeout" in result.run.error.lower()

    def test_generic_exception_in_run(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.side_effect = RuntimeError("unexpected boom")
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        assert result.success is False
        assert result.run.status == "error"
        assert "boom" in result.run.error

    def test_lock_contention_returns_skipped(self, runner_env, test_repo):
        """Pre-acquire the lock; run() should return skipped without calling subprocess."""
        lock = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        try:
            with patch("bluei.app.runner.subprocess.run") as mock_sub:
                result = runner_env["runner"].run(
                    test_repo, RunOptions(phase="issue-cycle", dry_run=False)
                )
                mock_sub.assert_not_called()
        finally:
            runner_env["runner"]._release_lock(lock)
        assert result.success is False
        assert result.run.status == "skipped"
        assert "already active" in result.run.error

    def test_run_persists_run_record(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        loaded = runner_env["state"].load_run("test-repo", result.run.id)
        assert loaded is not None
        assert loaded.status == "completed"

    def test_run_releases_lock_on_success(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        # Lock should be released — acquiring again should succeed
        lock = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert lock is not None
        runner_env["runner"]._release_lock(lock)

    def test_run_releases_lock_on_failure(self, runner_env, test_repo):
        proc = MagicMock(returncode=1, stdout="", stderr="fail")
        with patch("bluei.app.runner.subprocess.run", return_value=proc):
            runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        lock = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert lock is not None
        runner_env["runner"]._release_lock(lock)

    def test_run_releases_lock_on_exception(self, runner_env, test_repo):
        with patch(
            "bluei.app.runner.subprocess.run", side_effect=RuntimeError("kaboom")
        ):
            runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        lock = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert lock is not None
        runner_env["runner"]._release_lock(lock)

    def test_review_cycle_phase_delegates(self, runner_env, test_repo):
        """review-cycle phase should not call subprocess.run at all."""
        with (
            patch("bluei.app.runner.subprocess.run") as mock_sub,
            patch("bluei.app.runner.ReviewCycleEngine") as mock_rce,
        ):
            mock_result = MagicMock()
            mock_result.active_prs = 2
            mock_result.blocked_prs = 0
            mock_result.retry_eligible_prs = 0
            mock_result.retry_planned_prs = 0
            mock_result.retry_prepared_prs = 0
            mock_result.retry_executed_prs = 0
            mock_result.retry_failed_prs = 0
            mock_result.retry_exhausted_prs = 0
            mock_result.merge_ready_prs = 1
            mock_result.paused_prs = 0
            mock_result.findings_detected = 0
            mock_result.findings_published = 0
            mock_result.findings_failed = 0
            mock_rce.return_value.run.return_value = mock_result
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="review-cycle", dry_run=True)
            )
            mock_sub.assert_not_called()
            assert result.success is True
            assert "active_prs=2" in result.output

    def test_run_with_deterministic_backend(self, runner_env, test_repo):
        with patch("bluei.app.runner.subprocess.run") as mock_sub:
            mock_sub.return_value = _make_subprocess_ok()
            result = runner_env["runner"].run(
                test_repo,
                RunOptions(
                    phase="issue-cycle", dry_run=True, fix_engine="deterministic"
                ),
            )
        assert result.success is True
        assert result.run.fix_engine == "deterministic"

    def test_run_stderr_truncated_on_failure(self, runner_env, test_repo):
        long_error = "x" * 1000
        proc = MagicMock(returncode=1, stdout="", stderr=long_error)
        with patch("bluei.app.runner.subprocess.run", return_value=proc):
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        assert len(result.run.error) <= 500

    def test_persistence_failure_in_finally_does_not_mask_original_error(
        self, runner_env, test_repo
    ):
        """If save_run raises in finally, the original RuntimeError must survive
        and the lock must still be released."""
        with (
            patch(
                "bluei.app.runner.subprocess.run",
                side_effect=RuntimeError("original boom"),
            ),
            patch.object(
                runner_env["state"],
                "save_run",
                side_effect=OSError("disk full"),
            ),
        ):
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        # Original error preserved, persistence error not masked
        assert result.success is False
        assert result.run.status == "error"
        assert "original boom" in result.run.error
        assert "disk full" not in result.run.error
        # Lock released despite persistence failure
        lock = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert lock is not None
        runner_env["runner"]._release_lock(lock)

    def test_persistence_failure_in_finally_does_not_break_successful_run(
        self, runner_env, test_repo
    ):
        """A successful run must still return success even if save_run fails
        in the finally block."""
        with (
            patch(
                "bluei.app.runner.subprocess.run",
                return_value=_make_subprocess_ok(),
            ),
            patch.object(
                runner_env["state"],
                "save_run",
                side_effect=OSError("disk full"),
            ),
        ):
            result = runner_env["runner"].run(
                test_repo, RunOptions(phase="issue-cycle", dry_run=False)
            )
        assert result.success is True
        assert result.run.status == "completed"
        # Lock released despite persistence failure
        lock = runner_env["runner"]._acquire_lock("test-repo", "issue-cycle")
        assert lock is not None
        runner_env["runner"]._release_lock(lock)
