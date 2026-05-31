"""Integration tests: auto_tune -> runner._build_cli_args wiring.

Verifies that auto-tune computed overrides are actually applied as CLI
argument overrides when the runner builds its subprocess invocation.
No production code is modified.
"""

import json

import pytest

from bluei.app.auto_tune import (
    CONSECUTIVE_RETRY_FAILURE_THRESHOLD,
    compute_tune,
    flag_tune_success,
    read_tune_overrides,
)
from bluei.app.config import ConfigManager
from bluei.app.health import HealthEngine
from bluei.app.models import RepoConfig
from bluei.app.registry import RepoRegistry
from bluei.app.runner import RunEngine, RunOptions
from bluei.app.state import StateManager


# ── Helpers ──


def _make_stat(retry_failed=0, findings_failed=0, **extra):
    rec = {"retry_failed": retry_failed, "findings_failed": findings_failed}
    rec.update(extra)
    return rec


def _write_jsonl(path, records):
    lines = [json.dumps(r) for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _write_tune_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


# ── Fixtures ──


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "qa-agent"
    (ws / "repos").mkdir(parents=True)
    (ws / "plugins").mkdir()
    (ws / "logs").mkdir()
    (ws / "state").mkdir()
    return ws


@pytest.fixture
def runner_env(workspace):
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


def _get_arg_value(args, flag):
    return args[args.index(flag) + 1]


# ── Tests ──


class TestAutoTuneOverridesAppliedToCLIArgs:
    """Test 1: auto-tune overrides --max-prs-per-run in CLI args."""

    def test_max_prs_per_run_override_applied(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        stats_path = ws / "state" / "review_stats.jsonl"
        tune_path = ws / "state" / "auto_tune.json"

        _write_tune_state(tune_path, {"tuned_fields": {"max_prs_per_run": 8}})
        records = [
            _make_stat(retry_failed=1)
            for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)
        ]
        _write_jsonl(stats_path, records)

        overrides = compute_tune(stats_path, tune_path)
        assert "max_prs_per_run" in overrides

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_value = str(test_repo.config.limits.get("max_prs_per_run", 2))
        actual = _get_arg_value(args, "--max-prs-per-run")
        assert actual != default_value
        assert actual == str(overrides["max_prs_per_run"])

    def test_max_prs_per_run_specific_value(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        tune_path = ws / "state" / "auto_tune.json"

        _write_tune_state(
            tune_path,
            {
                "tuned_fields": {"max_prs_per_run": 1},
                "last_tune_ts": "2026-01-01T00:00:00Z",
            },
        )

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert _get_arg_value(args, "--max-prs-per-run") == "1"


class TestAutoTuneCooldownOverride:
    """Test 2: auto-tune overrides --finding-cooldown-seconds in CLI args."""

    def test_finding_cooldown_override_applied(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        stats_path = ws / "state" / "review_stats.jsonl"
        tune_path = ws / "state" / "auto_tune.json"

        records = [
            _make_stat(findings_failed=1)
            for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)
        ]
        _write_jsonl(stats_path, records)

        overrides = compute_tune(stats_path, tune_path)
        assert "finding_cooldown_seconds" in overrides

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_cooldown = str(test_repo.config.cooldowns.get("finding_seconds", 14400))
        actual = _get_arg_value(args, "--finding-cooldown-seconds")
        assert actual != default_cooldown
        assert actual == str(overrides["finding_cooldown_seconds"])

    def test_finding_cooldown_specific_value(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        tune_path = ws / "state" / "auto_tune.json"

        _write_tune_state(
            tune_path,
            {
                "tuned_fields": {"finding_cooldown_seconds": 43200},
                "last_tune_ts": "2026-01-01T00:00:00Z",
            },
        )

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert _get_arg_value(args, "--finding-cooldown-seconds") == "43200"


class TestAutoTuneRecovery:
    """Test 3: after flag_tune_success(), CLI args relax toward defaults."""

    def test_batch_recovers_toward_default(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        tune_path = ws / "state" / "auto_tune.json"

        _write_tune_state(
            tune_path,
            {
                "tuned_fields": {"max_prs_per_run": 1},
                "last_tune_ts": "2026-01-01T00:00:00Z",
            },
        )

        args_before = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert _get_arg_value(args_before, "--max-prs-per-run") == "1"

        flag_tune_success(tune_path)

        args_after = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        recovered = _get_arg_value(args_after, "--max-prs-per-run")
        default = str(test_repo.config.limits.get("max_prs_per_run", 2))
        assert recovered == default

    def test_cooldown_recovers_toward_default(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        tune_path = ws / "state" / "auto_tune.json"

        _write_tune_state(
            tune_path,
            {
                "tuned_fields": {"finding_cooldown_seconds": 28800},
                "last_tune_ts": "2026-01-01T00:00:00Z",
            },
        )

        flag_tune_success(tune_path)

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        default = str(test_repo.config.cooldowns.get("finding_seconds", 14400))
        assert _get_arg_value(args, "--finding-cooldown-seconds") == default

    def test_full_cycle_failure_then_success_recovery(self, runner_env, test_repo):
        ws = runner_env["workspace"]
        stats_path = ws / "state" / "review_stats.jsonl"
        tune_path = ws / "state" / "auto_tune.json"

        _write_tune_state(tune_path, {"tuned_fields": {"max_prs_per_run": 8}})
        records = [
            _make_stat(retry_failed=1, findings_failed=1)
            for _ in range(CONSECUTIVE_RETRY_FAILURE_THRESHOLD)
        ]
        _write_jsonl(stats_path, records)

        overrides = compute_tune(stats_path, tune_path)
        assert "max_prs_per_run" in overrides
        assert "finding_cooldown_seconds" in overrides

        args_tuned = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        assert _get_arg_value(args_tuned, "--max-prs-per-run") == str(
            overrides["max_prs_per_run"]
        )
        assert _get_arg_value(args_tuned, "--finding-cooldown-seconds") == str(
            overrides["finding_cooldown_seconds"]
        )

        flag_tune_success(tune_path)
        flag_tune_success(tune_path)

        args_recovered = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )
        default_batch = str(test_repo.config.limits.get("max_prs_per_run", 2))
        default_cooldown = str(test_repo.config.cooldowns.get("finding_seconds", 14400))
        assert _get_arg_value(args_recovered, "--max-prs-per-run") == default_batch
        assert (
            _get_arg_value(args_recovered, "--finding-cooldown-seconds")
            == default_cooldown
        )


class TestNoAutoTuneFileDefaults:
    """Test 4: without auto_tune.json, CLI args use default values."""

    def test_defaults_when_no_tune_file(self, runner_env, test_repo):
        tune_path = runner_env["workspace"] / "state" / "auto_tune.json"
        assert not tune_path.exists()

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_batch = str(test_repo.config.limits.get("max_prs_per_run", 2))
        default_cooldown = str(test_repo.config.cooldowns.get("finding_seconds", 14400))
        assert _get_arg_value(args, "--max-prs-per-run") == default_batch
        assert _get_arg_value(args, "--finding-cooldown-seconds") == default_cooldown

    def test_defaults_when_empty_tuned_fields(self, runner_env, test_repo):
        tune_path = runner_env["workspace"] / "state" / "auto_tune.json"
        _write_tune_state(tune_path, {"tuned_fields": {}, "last_tune_ts": None})

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_batch = str(test_repo.config.limits.get("max_prs_per_run", 2))
        assert _get_arg_value(args, "--max-prs-per-run") == default_batch


class TestCorruptedAutoTuneFileDefaults:
    """Test 5: corrupted auto_tune.json => no crash, defaults used."""

    def test_garbage_json_no_crash_defaults_used(self, runner_env, test_repo):
        tune_path = runner_env["workspace"] / "state" / "auto_tune.json"
        tune_path.parent.mkdir(parents=True, exist_ok=True)
        tune_path.write_text("THIS IS NOT JSON {{{}}}")

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_batch = str(test_repo.config.limits.get("max_prs_per_run", 2))
        default_cooldown = str(test_repo.config.cooldowns.get("finding_seconds", 14400))
        assert _get_arg_value(args, "--max-prs-per-run") == default_batch
        assert _get_arg_value(args, "--finding-cooldown-seconds") == default_cooldown

    def test_partial_json_no_crash(self, runner_env, test_repo):
        tune_path = runner_env["workspace"] / "state" / "auto_tune.json"
        tune_path.parent.mkdir(parents=True, exist_ok=True)
        tune_path.write_text('{"tuned_fields": {"max_prs_per_run": "NOT_A_NUMBER"}')

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_batch = str(test_repo.config.limits.get("max_prs_per_run", 2))
        assert _get_arg_value(args, "--max-prs-per-run") == default_batch

    def test_empty_file_no_crash(self, runner_env, test_repo):
        tune_path = runner_env["workspace"] / "state" / "auto_tune.json"
        tune_path.parent.mkdir(parents=True, exist_ok=True)
        tune_path.write_text("")

        args = runner_env["runner"]._build_cli_args(
            test_repo, RunOptions(phase="issue-cycle", dry_run=True)
        )

        default_batch = str(test_repo.config.limits.get("max_prs_per_run", 2))
        assert _get_arg_value(args, "--max-prs-per-run") == default_batch
