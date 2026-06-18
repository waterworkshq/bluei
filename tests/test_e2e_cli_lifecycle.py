#!/usr/bin/env python3
"""E2E CLI lifecycle tests — exercises every subcommand via in-process calls.

Runs bluei CLI command functions directly against a temp workspace and temp
Python repo. No network access, no gh auth, no real GitHub repos required.

Strategy: import _cmd_* functions from bin/bluei.py and call them with
monkeypatched BLUEI_DIR and WORKSPACE to isolate state into tmp_path.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _create_temp_python_repo(tmp_path: Path, git_commit_all_fn) -> Path:
    repo = tmp_path / "source-repo"
    repo.mkdir(parents=True, exist_ok=True)

    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("")

    (repo / "src" / "violations.py").write_text(
        "import os\n"
        "\n"
        "\n"
        "def process(items):\n"
        "    for idx, item in enumerate(items):\n"
        "        entry = items.pop(0)\n"
        "        if entry is None:\n"
        "            break\n"
        "    try:\n"
        "        with open('/tmp/bluei_test_data.txt') as f:\n"
        "            data = f.read()\n"
        "    except Exception:\n"
        "        data = ''\n"
        "    return data  \n"
    )

    (repo / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='test-bluei-e2e', version='0.1.0')\n"
    )

    (repo / "README.md").write_text("# test-bluei-e2e\n")

    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@bluei.dev"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Bluei Test"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    git_commit_all_fn(repo, "initial commit")
    return repo


def _create_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    for d in ("repos", "logs", "state"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    plugins_dir = ws / "plugins"
    real_plugins = ROOT / "plugins"
    if real_plugins.exists():
        os.symlink(str(real_plugins), str(plugins_dir))
    else:
        plugins_dir.mkdir(parents=True, exist_ok=True)
    templates_dir = ws / "templates"
    real_templates = ROOT / "templates"
    if real_templates.exists():
        os.symlink(str(real_templates), str(templates_dir))
    else:
        (templates_dir / "repos").mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def e2e_env(tmp_path, git_commit_all):
    workspace = _create_workspace(tmp_path)
    repo_path = _create_temp_python_repo(tmp_path, git_commit_all)
    return {
        "workspace": workspace,
        "repo_path": repo_path,
        "repo_name": "e2e-test-repo",
    }


def _patch_workspace(ev: dict, monkeypatch):
    import bluei.app.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "WORKSPACE", ev["workspace"])
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(ev["workspace"]))
    monkeypatch.setenv("BLUEI_MODE", "observe")

    import bin.bluei as bluei_main

    monkeypatch.setattr(bluei_main, "BLUEI_DIR", ev["workspace"])


def _state_dir(ev: dict) -> Path:
    return ev["workspace"] / "repos" / ev["repo_name"] / "state"


def _onboard_args(ev: dict) -> list[str]:
    return [
        "--repo",
        str(ev["repo_path"]),
        "--name",
        ev["repo_name"],
        "--mode",
        "observe",
        "--profile",
        "conservative",
    ]


def _do_onboard(ev, monkeypatch):
    _patch_workspace(ev, monkeypatch)
    from bin.bluei import _cmd_onboard

    return _cmd_onboard(_onboard_args(ev))


def _do_run(ev, monkeypatch):
    _patch_workspace(ev, monkeypatch)
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry
    from bluei.app.state import StateManager
    from bluei.app.health import HealthEngine
    from bluei.app.runner import RunEngine, RunOptions

    config_mgr = ConfigManager(ev["workspace"])
    registry = RepoRegistry(config_mgr)
    repo = registry.find_by_name(ev["repo_name"])
    assert repo is not None
    state = StateManager(config_mgr.repos_dir)
    engine = RunEngine(registry, state, HealthEngine(), config_mgr)
    engine.runner_path = ROOT / "bluei" / "engine" / "cli.py"
    return engine.run(repo, RunOptions(phase="issue-cycle", dry_run=True))


class TestInit:
    def test_init_registers_repo(self, e2e_env, monkeypatch):
        ev = e2e_env
        _patch_workspace(ev, monkeypatch)
        from bin.bluei import _cmd_init

        ret = _cmd_init(["--path", str(ev["repo_path"]), "--name", ev["repo_name"]])
        assert ret == 0

        registry_file = ev["workspace"] / "registry.yaml"
        assert registry_file.exists()
        content = registry_file.read_text()
        assert ev["repo_name"] in content


class TestOnboard:
    def test_onboard_detects_language_and_creates_config(self, e2e_env, monkeypatch):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        config_file = ev["workspace"] / "repos" / ev["repo_name"] / "config.yaml"
        assert config_file.exists()
        config_text = config_file.read_text()
        assert "python" in config_text

        registry_file = ev["workspace"] / "registry.yaml"
        assert registry_file.exists()
        assert ev["repo_name"] in registry_file.read_text()


class TestStatus:
    def test_status_shows_repo(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        from bin.bluei import _cmd_status

        ret = _cmd_status([])
        assert ret == 0
        assert ev["repo_name"] in capsys.readouterr().out


class TestDoctor:
    def test_doctor_checks(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        _patch_workspace(ev, monkeypatch)
        from bin.bluei import _cmd_doctor

        ret = _cmd_doctor([])
        assert ret == 0
        out = capsys.readouterr().out
        assert "workspace" in out.lower()
        assert "python" in out.lower()


class TestDuster:
    def test_duster_finds_violations(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        result = _do_run(ev, monkeypatch)
        assert result.success

        sd = _state_dir(ev)
        findings_file = sd / "findings.jsonl"
        if findings_file.exists():
            lines = [l for l in findings_file.read_text().splitlines() if l.strip()]
            assert len(lines) > 0


class TestRun:
    def test_run_dry_run_writes_state(self, e2e_env, monkeypatch):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        result = _do_run(ev, monkeypatch)
        sd = _state_dir(ev)
        status_file = sd / "status.json"
        assert status_file.exists()


class TestStateAccumulation:
    def test_second_run_accumulates_state(self, e2e_env, monkeypatch):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        _do_run(ev, monkeypatch)
        _do_run(ev, monkeypatch)

        runs_dir = _state_dir(ev) / "runs"
        if runs_dir.exists():
            run_files = list(runs_dir.glob("*.json"))
            assert len(run_files) >= 2


class TestCampaign:
    def test_campaign_plan_from_findings(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0
        _do_run(ev, monkeypatch)

        from bluei.app.config import ConfigManager

        config_mgr = ConfigManager(ev["workspace"])

        from bin.bluei import _cmd_campaign

        ret = _cmd_campaign(
            [
                "plan",
                "--repo",
                ev["repo_name"],
                "--state-root",
                str(config_mgr.repos_dir),
            ]
        )
        assert ret in (0, 2)


class TestEmergent:
    def test_emergent_propose_from_findings(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0
        _do_run(ev, monkeypatch)

        from bluei.app.config import ConfigManager

        config_mgr = ConfigManager(ev["workspace"])

        from bin.bluei import _cmd_emergent

        ret = _cmd_emergent(
            [
                "propose",
                "--repo",
                ev["repo_name"],
                "--state-root",
                str(config_mgr.repos_dir),
            ]
        )
        assert ret == 0
        out = capsys.readouterr().out
        assert "Emergent proposals:" in out


class TestPatterns:
    def test_patterns_list_empty(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        from bin.bluei import _cmd_patterns

        ret = _cmd_patterns(["list", "--repo", ev["repo_name"]])
        assert ret == 0
        out = capsys.readouterr().out
        assert "No active patterns found" in out or "patterns" in out.lower()


class TestLesson:
    def test_lesson_add_and_list(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0

        from bin.bluei import _cmd_lesson

        ret = _cmd_lesson(
            [
                "add",
                "--repo",
                ev["repo_name"],
                "--worked",
                "E2E test lesson entry",
            ]
        )
        assert ret == 0
        assert "Lesson added" in capsys.readouterr().out

        ret = _cmd_lesson(["list", "--repo", ev["repo_name"]])
        assert ret == 0
        assert "E2E test lesson entry" in capsys.readouterr().out


class TestFullChain:
    def test_full_chain_init_to_report(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0
        assert "python" in capsys.readouterr().out.lower()

        config_file = ev["workspace"] / "repos" / ev["repo_name"] / "config.yaml"
        assert config_file.exists()

        from bin.bluei import _cmd_status

        ret = _cmd_status([])
        assert ret == 0
        assert ev["repo_name"] in capsys.readouterr().out

        result = _do_run(ev, monkeypatch)
        assert result.success

        sd = _state_dir(ev)
        assert (sd / "status.json").exists()

        from bin.bluei import _cmd_doctor

        ret = _cmd_doctor([])
        assert ret == 0

    def test_full_chain_init_onboard_scan_report(self, e2e_env, monkeypatch, capsys):
        """Full lifecycle: init → onboard → scan → report (JSON).

        Verifies each CLI stage composes with the next: init registers the
        repo, onboard runs plugin discovery + writes config, scan produces
        findings/status, report reads that state and emits structured JSON.
        """
        ev = e2e_env

        # ── 1. init: quick registration ──
        _patch_workspace(ev, monkeypatch)
        from bin.bluei import _cmd_init

        ret = _cmd_init(["--path", str(ev["repo_path"]), "--name", ev["repo_name"]])
        assert ret == 0, "init should succeed"
        registry_file = ev["workspace"] / "registry.yaml"
        assert registry_file.exists()
        assert ev["repo_name"] in registry_file.read_text()

        # init registers the repo, which would make onboard refuse duplicates.
        # Reset the registry + repo dir so onboard can run the full pipeline.
        registry_file.unlink(missing_ok=True)
        repo_state_dir = ev["workspace"] / "repos" / ev["repo_name"]
        if repo_state_dir.exists():
            shutil.rmtree(repo_state_dir)

        # ── 2. onboard: plugin discovery + config.yaml ──
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0, "onboard should succeed after init state reset"
        config_file = ev["workspace"] / "repos" / ev["repo_name"] / "config.yaml"
        assert config_file.exists()
        assert "python" in config_file.read_text().lower()

        # ── 3. scan: produces findings.jsonl + status.json ──
        result = _do_run(ev, monkeypatch)
        assert result.success, "scan (issue-cycle dry-run) should succeed"
        sd = _state_dir(ev)
        assert (sd / "status.json").exists()
        assert (sd / "findings.jsonl").exists()

        # ── 4. report: JSON output composes over scan state ──
        capsys.readouterr()  # drain any prior stdout from init/onboard/scan
        from bin.bluei import _cmd_report

        ret = _cmd_report(ev["repo_name"], ["--format", "json"])
        assert ret == 0, "report --format json should succeed"

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["repo"]["name"] == ev["repo_name"]
        assert "summary" in data
        assert "health_trend" in data
        assert "generated_at" in data
        assert isinstance(data["summary"]["total_findings"], int)
        # The scan produced findings → report should reflect them
        assert data["summary"]["total_findings"] > 0


class TestHealth:
    """bluei health <name> — vitality score computation.

    _cmd_health delegates to the engine subprocess via _run_engine; this test
    pins the dispatch contract and exercises the real HealthEngine against
    findings produced by an actual scan.
    """

    def test_health_computes_score(self, e2e_env, monkeypatch):
        ev = e2e_env
        # Onboard + scan to produce real findings in state
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0
        result = _do_run(ev, monkeypatch)
        assert result.success

        # ── Dispatch contract: _cmd_health forwards to _run_engine ──
        import bin.bluei as bluei_main

        captured = {}

        def fake_run_engine(args):
            captured["args"] = list(args)
            return 0

        monkeypatch.setattr(bluei_main, "_run_engine", fake_run_engine)

        from bin.bluei import _cmd_health

        ret = _cmd_health(ev["repo_name"], [])
        assert ret == 0
        # The CLI must ask the engine to compute health for this repo
        assert captured["args"] == ["health", "--repo", ev["repo_name"]]

        # Passthrough args must be forwarded verbatim
        ret = _cmd_health(ev["repo_name"], ["--verbose"])
        assert ret == 0
        assert captured["args"] == ["health", "--repo", ev["repo_name"], "--verbose"]

        # ── Computation contract: HealthEngine yields a valid score ──
        from bluei.app.config import ConfigManager
        from bluei.app.health import HealthEngine
        from bluei.app.state import StateManager

        config_mgr = ConfigManager(ev["workspace"])
        state = StateManager(config_mgr.repos_dir)
        findings = state.load_findings(ev["repo_name"])

        engine = HealthEngine()
        health = engine.calculate(findings)

        # Overall score is a bounded float
        assert isinstance(health.score, float)
        assert 0.0 <= health.score <= 100.0

        # All 9 granular components + the code_quality aggregate must be present
        expected_components = {
            "bug_quality",
            "lint_quality",
            "technical_debt",
            "documentation",
            "performance",
            "test_gaps",
            "test_coverage",
            "type_safety",
            "maintainability",
            "code_quality",
        }
        assert expected_components.issubset(health.components.keys())
        for name, val in health.components.items():
            assert 0 <= val <= 100, f"component {name}={val} out of [0, 100]"

        # When findings exist, the engine must actually penalize something.
        # The test repo triggers lint/perf rules, so those buckets should dip.
        if findings:
            penalized = [
                c
                for c in (
                    "lint_quality",
                    "performance",
                    "bug_quality",
                    "maintainability",
                    "technical_debt",
                )
                if health.components.get(c, 100.0) < 100.0
            ]
            assert penalized, (
                f"no component penalized despite {len(findings)} findings "
                f"(rules={[f.rule for f in findings]})"
            )


class TestReport:
    """bluei report <name> --format json — structured report over scan state."""

    def test_report_json_output(self, e2e_env, monkeypatch, capsys):
        ev = e2e_env
        ret = _do_onboard(ev, monkeypatch)
        assert ret == 0
        result = _do_run(ev, monkeypatch)
        assert result.success

        capsys.readouterr()  # drain onboard/run stdout before report
        from bin.bluei import _cmd_report

        ret = _cmd_report(ev["repo_name"], ["--format", "json"])
        assert ret == 0

        out = capsys.readouterr().out
        data = json.loads(out)

        # ── Top-level shape ──
        for key in ("repo", "summary", "health_trend", "generated_at"):
            assert key in data, f"report JSON missing top-level key: {key}"

        # ── repo subsection ──
        assert data["repo"]["name"] == ev["repo_name"]
        assert "path" in data["repo"]
        assert "health_score" in data["repo"]
        assert "language" in data["repo"]

        # ── summary subsection (integer counters) ──
        summary = data["summary"]
        for key in ("total_findings", "open_issues", "open_prs"):
            assert key in summary, f"summary missing {key}"
            assert isinstance(summary[key], int), (
                f"summary.{key} must be int, got {type(summary[key])}"
            )

        # ── health_trend is a list (possibly empty pre-history) ──
        assert isinstance(data["health_trend"], list)

        # ── generated_at is a timestamp string ──
        assert isinstance(data["generated_at"], str)
        assert len(data["generated_at"]) > 0

        # ── Cross-check: total_findings reflects findings.jsonl on disk ──
        findings_file = _state_dir(ev) / "findings.jsonl"
        assert findings_file.exists()
        findings_lines = [
            line for line in findings_file.read_text().splitlines() if line.strip()
        ]
        assert summary["total_findings"] == len(findings_lines)
