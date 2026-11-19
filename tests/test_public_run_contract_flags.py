"""Public `bluei run` contract: --deterministic-only and --baseline-checks.

Covers the documented public-command behavior without a real model backend:
--deterministic-only must force the deterministic engine (never a model), and
--baseline-checks must be forwarded verbatim, overriding repo config, with
missing/repeated values rejected by the dispatcher.
"""
from argparse import Namespace
from types import SimpleNamespace

from bin import bluei as cli
from bluei.app.config import ConfigManager
from bluei.app.health import HealthEngine
from bluei.app.models import RepoConfig
from bluei.app.registry import RepoRegistry
from bluei.app.runner import RunEngine, RunOptions, RunResult
from bluei.app.state import StateManager


def _runner(tmp_path):
    config = ConfigManager(tmp_path)
    registry = RepoRegistry(config)
    runner = RunEngine(registry, StateManager(config.repos_dir), HealthEngine(), config)
    repo = SimpleNamespace(
        config=RepoConfig(
            id="test", name="fixture", path=str(tmp_path / "source"), language="python"
        )
    )
    return runner, repo


def _public_repo(tmp_path, monkeypatch, name="contract"):
    from bluei.app import config as config_module

    workspace = tmp_path / "workspace"
    manager = ConfigManager(workspace)
    monkeypatch.setattr(config_module, "WORKSPACE", workspace)
    registry = RepoRegistry(manager)
    target = tmp_path / "target"
    target.mkdir()
    return manager, registry, registry.create_from_path(
        target, name=name, language="python"
    )


def _capture_public_run(monkeypatch):
    captured = {}

    def fake_run(self, repo, options):
        captured["options"] = options
        return RunResult(
            run=SimpleNamespace(scan_complete=True, findings_detected=0),
            success=True,
            output="",
            error=None,
        )

    monkeypatch.setattr(RunEngine, "run", fake_run)
    return captured


# ── runner arg construction ────────────────────────────────────


def test_deterministic_only_flag_forwarded_to_engine_args(tmp_path):
    runner, repo = _runner(tmp_path)
    args = runner._build_cli_args(
        repo,
        RunOptions(dry_run=False, fix_engine="deterministic", deterministic_only=True),
        {"logical_backend": "deterministic", "runner_engine": "deterministic", "template": None},
    )
    assert args[args.index("--fix-engine") + 1] == "deterministic"
    assert "--deterministic-only" in args
    assert "--claude-cmd-template" not in args


def test_deterministic_only_absent_by_default(tmp_path):
    runner, repo = _runner(tmp_path)
    args = runner._build_cli_args(repo, RunOptions(dry_run=False))
    assert "--deterministic-only" not in args


def test_baseline_override_forwarded_verbatim_and_wins_over_config(tmp_path):
    runner, repo = _runner(tmp_path)
    repo.config.baseline_checks = [["make", "check"]]
    raw = '[["npm","test"],["npm","run","build"]]'
    args = runner._build_cli_args(repo, RunOptions(baseline_checks=raw))
    assert args.count("--baseline-checks") == 1
    assert args[args.index("--baseline-checks") + 1] == raw


def test_baseline_config_still_used_without_override(tmp_path):
    runner, repo = _runner(tmp_path)
    repo.config.baseline_checks = [["make", "check"]]
    args = runner._build_cli_args(repo, RunOptions())
    assert args[args.index("--baseline-checks") + 1] == '[["make", "check"]]'


# ── public dispatcher contract ─────────────────────────────────


def test_public_deterministic_only_forces_deterministic_never_model(tmp_path, monkeypatch):
    manager, registry, repo = _public_repo(tmp_path, monkeypatch)
    repo.config.fix_engine = "claude"
    repo.config.fallback_engines = ["claude", "opencode"]
    captured = _capture_public_run(monkeypatch)
    code = cli._cmd_run(
        "contract",
        ["--phase", "issue-cycle", "--dry-run", "--deterministic-only", "--fix-engine", "claude"],
    )
    assert code == 0
    options = captured["options"]
    assert options.deterministic_only is True
    assert options.fix_engine == "deterministic"
    engine = RunEngine(registry, StateManager(manager.repos_dir), HealthEngine(), manager)
    resolved = engine._resolve_backend(repo, options.fix_engine)
    assert resolved["logical_backend"] == "deterministic"
    assert resolved["template"] is None


def test_public_baseline_checks_forwarded_verbatim(tmp_path, monkeypatch):
    manager, registry, repo = _public_repo(tmp_path, monkeypatch)
    repo.config.baseline_checks = [["make", "check"]]
    captured = _capture_public_run(monkeypatch)
    code = cli._cmd_run(
        "contract", ["--phase", "issue-cycle", "--dry-run", "--baseline-checks", "not json"]
    )
    assert code == 0
    assert captured["options"].baseline_checks == "not json"


def test_engine_preserves_malformed_baseline_as_unavailable():
    from bluei.engine.commands.parse_args import resolve_baseline_checks

    assert resolve_baseline_checks(Namespace(baseline_checks="not json")) == {
        "baseline-invalid": []
    }
    assert resolve_baseline_checks(Namespace(baseline_checks='{"a": 1}')) == {
        "baseline-invalid": []
    }


def test_public_run_rejects_baseline_checks_without_value(tmp_path, monkeypatch, capsys):
    _public_repo(tmp_path, monkeypatch)
    _capture_public_run(monkeypatch)
    code = cli._cmd_run("contract", ["--phase", "issue-cycle", "--dry-run", "--baseline-checks"])
    assert code == 2
    assert "--baseline-checks requires exactly one value" in capsys.readouterr().err


def test_public_run_rejects_repeated_baseline_checks(tmp_path, monkeypatch, capsys):
    _public_repo(tmp_path, monkeypatch)
    _capture_public_run(monkeypatch)
    code = cli._cmd_run(
        "contract",
        ["--phase", "issue-cycle", "--dry-run",
         "--baseline-checks", "[]", "--baseline-checks", "[]"],
    )
    assert code == 2
    assert "--baseline-checks requires exactly one value" in capsys.readouterr().err


def test_public_run_rejects_deterministic_only_with_finding_id(tmp_path, monkeypatch, capsys):
    _public_repo(tmp_path, monkeypatch)
    _capture_public_run(monkeypatch)
    code = cli._cmd_run(
        "contract",
        ["--phase", "orchestrated", "--dry-run", "--discovery-scope", "python",
         "--finding-id", "f-1", "--fix-engine", "claude", "--deterministic-only"],
    )
    assert code == 2
    assert "--finding-id requires" in capsys.readouterr().err
