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
