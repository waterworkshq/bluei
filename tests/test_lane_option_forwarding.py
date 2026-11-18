"""Public lane options preserve logical backend and explicit approval boundaries."""

from types import SimpleNamespace

import pytest

from bluei.app.config import ConfigManager
from bluei.app.health import HealthEngine
from bluei.app.models import RepoConfig
from bluei.app.registry import RepoRegistry
from bluei.app.runner import RunEngine, RunOptions
from bluei.app.state import StateManager


def _runner(tmp_path):
    config = ConfigManager(tmp_path)
    registry = RepoRegistry(config)
    runner = RunEngine(registry, StateManager(config.repos_dir), HealthEngine(), config)
    repo = SimpleNamespace(config=RepoConfig(id="test", name="fixture", path=str(tmp_path / "source"), language="python"))
    return runner, repo


def test_ordinary_opencode_preserves_backend_identity_and_template(tmp_path):
    runner, repo = _runner(tmp_path)
    args = runner._build_cli_args(
        repo, RunOptions(dry_run=False),
        {"logical_backend": "opencode", "runner_engine": "claude", "template": "inert-opencode {prompt_file}"},
    )
    assert args[args.index("--fix-engine") + 1] == "opencode"
    assert args[args.index("--claude-cmd-template") + 1] == "inert-opencode {prompt_file}"


def test_explicit_structural_approval_and_batch_forwarding(tmp_path):
    runner, repo = _runner(tmp_path)
    args = runner._build_cli_args(
        repo, RunOptions(phase="refactor-cycle", approve_refactor_item="rw-example", max_queue_items=1),
        {"logical_backend": "deterministic", "runner_engine": "deterministic", "template": None},
    )
    assert args[args.index("--approve-refactor-item") + 1] == "rw-example"
    assert args[args.index("--max-queue-items") + 1] == "1"
    assert "--auto-approve" not in args
    ordinary = runner._build_cli_args(repo, RunOptions(batch_pr_enabled=True, fix_engine="deterministic"))
    assert "--batch-pr-enabled" in ordinary


def test_approval_is_not_forwarded_to_another_phase(tmp_path):
    runner, repo = _runner(tmp_path)
    with pytest.raises(ValueError, match="refactor-cycle"):
        runner._build_cli_args(repo, RunOptions(approve_refactor_item="rw-example"))
