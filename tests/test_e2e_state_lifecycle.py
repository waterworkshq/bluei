import json
import shutil
from pathlib import Path

import yaml

from bluei.app.config import ConfigManager
from bluei.app.healer import TransientArtifactHealer
from bluei.app.models import RepoConfig, Repo, Run, generate_id, now_iso
from bluei.app.registry import RepoRegistry
from bluei.app.state import StateManager
from bluei.engine.state import load_state, save_state, repair_state


def _make_config(workspace: Path, name: str = "test-repo") -> RepoConfig:
    return RepoConfig(
        id=f"repo-{name}",
        name=name,
        path=str(workspace / name),
        language="python",
        enabled=True,
        safety={"mode": "observe", "profile": "conservative"},
    )


def _make_run(repo_id: str) -> Run:
    return Run(
        id=generate_id("run"),
        repo_id=repo_id,
        phase="discover",
        started_at=now_iso(),
        dry_run=True,
    )


class TestOnboardCreatesStateDirs:
    def test_state_dir_created(self, tmp_path):
        cfg = ConfigManager(tmp_path)
        reg = RepoRegistry(cfg)
        config = _make_config(tmp_path)
        reg.create(config)
        state_dir = tmp_path / "repos" / "test-repo" / "state"
        assert state_dir.is_dir()


class TestStateSelfHealsFromCorruption:
    def test_corrupt_state_json_loads_defaults_and_rescues(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{corrupted junk!!!")
        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0
        repaired = repair_state(state_file)
        assert repaired is True
        save_state(state_file, state)
        reloaded = load_state(state_file)
        assert reloaded["open_issues"] == 0


class TestMultiRunStateAccumulation:
    def test_five_runs_create_five_files(self, tmp_path):
        cfg = ConfigManager(tmp_path)
        reg = RepoRegistry(cfg)
        config = _make_config(tmp_path)
        reg.create(config)
        sm = StateManager(cfg.repos_dir)
        repo_name = "test-repo"
        for _ in range(5):
            run = _make_run(config.id)
            sm.save_run(repo_name, run)
        runs_dir = tmp_path / "repos" / repo_name / "runs"
        json_files = list(runs_dir.glob("*.json"))
        assert len(json_files) == 5


class TestDeleteRecreateFreshState:
    def test_delete_then_recreate_is_fresh(self, tmp_path):
        cfg = ConfigManager(tmp_path)
        reg = RepoRegistry(cfg)
        config = _make_config(tmp_path)
        reg.create(config)
        repo_name = "test-repo"
        sm = StateManager(cfg.repos_dir)
        run = _make_run(config.id)
        sm.save_run(repo_name, run)
        sm.save_state(repo_name, {"open_issues": 42, "open_prs": 7})
        reg.update(repo_name, {"status": "running", "total_fixes": 99})
        reg.delete(repo_name)
        assert not (tmp_path / "repos" / repo_name).exists()
        config2 = _make_config(tmp_path)
        reg.create(config2)
        state_dir = tmp_path / "repos" / repo_name / "state"
        assert state_dir.is_dir()
        state_file = state_dir / "state.json"
        assert not state_file.exists()
        loaded_state = sm.load_state(repo_name)
        assert loaded_state["open_issues"] == 0
        runs_dir = tmp_path / "repos" / repo_name / "runs"
        assert not any(runs_dir.glob("*.json"))


class TestConfigV1MigrationPersists:
    def test_v1_config_gets_safety_field_after_round_trip(self, tmp_path):
        cfg = ConfigManager(tmp_path)
        config_dir = tmp_path / "repos" / "test-repo"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        v1_data = {
            "id": "repo-test-repo",
            "name": "test-repo",
            "path": "/tmp/test-repo",
            "language": "python",
        }
        with open(config_path, "w") as f:
            yaml.dump(v1_data, f)
        loaded = cfg.load_repo_config("test-repo")
        assert loaded is not None
        assert "safety" in loaded.to_dict()
        assert loaded.safety["mode"] == "observe"
        cfg.save_repo_config(loaded)
        reloaded = cfg.load_repo_config("test-repo")
        assert reloaded is not None
        assert reloaded.safety["mode"] == "observe"
        assert reloaded.safety["profile"] == "conservative"


class TestConcurrentRegistryUpdate:
    def test_sequential_saves_preserve_data(self, tmp_path):
        cfg = ConfigManager(tmp_path)
        reg = RepoRegistry(cfg)
        config = _make_config(tmp_path)
        reg.create(config)
        repo_name = "test-repo"
        state_file = cfg.repos_dir / repo_name / "state" / "repo_state.json"
        reg._save_repo_state_file(state_file, {"status": "running", "total_fixes": 10})
        reg._save_repo_state_file(
            state_file, {"status": "idle", "total_fixes": 20, "total_prs": 5}
        )
        loaded = reg._load_repo_state_file(state_file)
        assert loaded["status"] == "idle"
        assert loaded["total_fixes"] == 20
        assert loaded["total_prs"] == 5


class TestJsonlRotationByLines:
    def test_rotation_triggers_at_10001_lines(self, tmp_path):
        sm = StateManager(tmp_path / "repos")
        repo_name = "rot-test"
        sm.get_review_events_file(repo_name).parent.mkdir(parents=True, exist_ok=True)
        events_file = sm.get_review_events_file(repo_name)
        for i in range(10001):
            with open(events_file, "a") as f:
                f.write(json.dumps({"idx": i}) + "\n")
        from bluei.app.state import _rotate_jsonl_if_needed

        _rotate_jsonl_if_needed(events_file)
        assert not events_file.exists()
        bak = events_file.with_suffix(".jsonl.bak")
        assert bak.exists()
        with open(bak) as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 10001


class TestTransientArtifactRemoval:
    def test_pycache_removed(self, tmp_path):
        import subprocess

        repo_dir = tmp_path / "sample-repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo_dir),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo_dir),
            capture_output=True,
        )
        (repo_dir / "main.py").write_text("print('hello')\n")
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo_dir),
            capture_output=True,
        )
        pycache = repo_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "mod.cpython-312.pyc").write_bytes(b"\x00" * 10)
        healer = TransientArtifactHealer(repo_dir)
        result = healer.heal(remove_artifacts=True, dry_run=False)
        assert result["artifacts_changed"] is True
        assert not pycache.exists()


class TestRepairHandlesEmptyFile:
    def test_empty_json_returns_defaults(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("")
        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0
        assert state["created"] == []
        assert state["finding_activity"] == {}

    def test_repair_state_fixes_empty_file(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("")
        repaired = repair_state(state_file)
        assert repaired is True
        state = load_state(state_file)
        assert state["open_issues"] == 0


class TestRepairHandlesTruncatedJson:
    def test_truncated_json_returns_defaults(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text('{"open_issues": 5, "open_prs')
        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0

    def test_repair_state_fixes_truncated_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text('{"open_issues": 99, "created": [1,')
        repaired = repair_state(state_file)
        assert repaired is True
        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert isinstance(state["created"], list)
