"""Integration tests for the registry → app state → engine state chain.

Tests that RepoRegistry, StateManager, and engine.state functions operate
on the same directory structure and produce consistent, interoperable data.
"""

import json
from pathlib import Path

import pytest

from bluei.app.config import ConfigManager
from bluei.app.models import RepoConfig
from bluei.app.registry import RepoRegistry
from bluei.app.state import StateManager
from bluei.engine.models import Finding
from bluei.engine.state import (
    append_findings,
    load_findings_seen,
    load_state,
    save_state,
)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "qa-agent"
    (ws / "repos").mkdir(parents=True)
    (ws / "plugins").mkdir()
    (ws / "logs").mkdir()
    yield ws


@pytest.fixture
def config(workspace):
    return ConfigManager(workspace)


@pytest.fixture
def registry(config):
    return RepoRegistry(config)


@pytest.fixture
def state_manager(config):
    return StateManager(config.repos_dir)


def _make_engine_finding(**overrides):
    defaults = {
        "finding_id": "f-chain-001",
        "repo": "/tmp/test-chain",
        "path": "src/main.py",
        "line": 42,
        "rule": "unused-import",
        "snippet": "import os",
        "confidence": 0.9,
        "quick_win": True,
        "safe_to_autofix": True,
    }
    defaults.update(overrides)
    return Finding(**defaults)


def _register_repo(registry, name="chain-repo"):
    config = RepoConfig(
        id=f"repo-{name}",
        name=name,
        path=f"/tmp/{name}",
        language="python",
    )
    return registry.create(config)


class TestEngineStateRoundTrip:
    """registry.create() → StateManager.get_state_file() → engine load/save → StateManager.load_state()"""

    def test_fresh_repo_engine_state_round_trip(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        assert state_file == (
            registry.config.repos_dir / repo_name / "state" / "state.json"
        )

        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0
        assert state["created"] == []
        assert state["finding_activity"] == {}
        assert "reconciliation_events" in state

        state["open_issues"] = 5
        state["open_prs"] = 3
        state["created"] = ["pr-100"]
        state["finding_activity"] = {"f1": {"last_action": "fix"}}
        save_state(state_file, state)

        loaded_by_app = state_manager.load_state(repo_name)
        assert loaded_by_app["open_issues"] == 5
        assert loaded_by_app["open_prs"] == 3
        assert loaded_by_app["created"] == ["pr-100"]
        assert loaded_by_app["finding_activity"]["f1"]["last_action"] == "fix"


class TestFindingsRoundTrip:
    """registry.create() → engine.append_findings() → engine.load_findings_seen()"""

    def test_fresh_repo_findings_round_trip(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        findings_file = state_manager.get_findings_file(repo_name)
        assert findings_file == (
            registry.config.repos_dir / repo_name / "state" / "findings.jsonl"
        )

        f1 = _make_engine_finding(finding_id="f-alpha")
        f2 = _make_engine_finding(finding_id="f-beta", rule="print-left-in")
        written = append_findings(findings_file, [f1, f2])
        assert written == 2

        seen = load_findings_seen(findings_file)
        assert seen == {"f-alpha", "f-beta"}

        duplicate_written = append_findings(findings_file, [f1])
        assert duplicate_written == 0

        new_finding = _make_engine_finding(finding_id="f-gamma")
        written2 = append_findings(findings_file, [new_finding])
        assert written2 == 1

        seen2 = load_findings_seen(findings_file)
        assert seen2 == {"f-alpha", "f-beta", "f-gamma"}


class TestStateFilePathConsistency:
    """Verify that StateManager and engine resolve the same paths."""

    def test_state_file_paths_match(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        app_state_file = state_manager.get_state_file(repo_name)
        engine_expected = registry.config.repos_dir / repo_name / "state" / "state.json"
        assert app_state_file == engine_expected

        assert app_state_file.exists() is False
        state = load_state(app_state_file)
        assert state["open_issues"] == 0
        save_state(app_state_file, state)
        assert app_state_file.exists()

    def test_findings_file_paths_match(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        app_findings = state_manager.get_findings_file(repo_name)
        engine_expected = (
            registry.config.repos_dir / repo_name / "state" / "findings.jsonl"
        )
        assert app_findings == engine_expected

    def test_issues_file_paths_match(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        app_issues = state_manager.get_issues_file(repo_name)
        engine_expected = (
            registry.config.repos_dir / repo_name / "state" / "issues.json"
        )
        assert app_issues == engine_expected


class TestDeleteAndRecreate:
    """Delete a repo, re-register with same name, verify state is fresh."""

    def test_delete_recreate_no_stale_state(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        save_state(
            state_file,
            {
                "open_issues": 99,
                "open_prs": 88,
                "created": ["stale-pr"],
                "finding_activity": {"old": {"last_action": "x"}},
            },
        )

        findings_file = state_manager.get_findings_file(repo_name)
        f = _make_engine_finding(finding_id="f-stale")
        append_findings(findings_file, [f])

        assert state_file.exists()
        assert findings_file.exists()

        registry.delete(repo_name)
        assert not state_file.exists()
        assert not findings_file.exists()

        _register_repo(registry)

        new_state = load_state(state_file)
        assert new_state["open_issues"] == 0
        assert new_state["open_prs"] == 0
        assert new_state["created"] == []
        assert new_state["finding_activity"] == {}

        new_seen = load_findings_seen(findings_file)
        assert new_seen == set()


class TestCrossLayerFindingSerialization:
    """Write via engine Finding.as_dict() to findings.jsonl → read back."""

    def test_engine_finding_serialization_jsonl(
        self, registry, state_manager, tmp_path
    ):
        _register_repo(registry)
        repo_name = "chain-repo"

        findings_file = state_manager.get_findings_file(repo_name)
        finding = _make_engine_finding(
            finding_id="f-cross-1",
            repo="/tmp/chain-repo",
            path="src/utils.py",
            line=15,
            rule="unused-variable",
            snippet="x = 42",
            confidence=0.75,
            quick_win=False,
            safe_to_autofix=False,
        )

        written = append_findings(findings_file, [finding])
        assert written == 1

        seen = load_findings_seen(findings_file)
        assert "f-cross-1" in seen

        raw_lines = findings_file.read_text().strip().splitlines()
        assert len(raw_lines) == 1

        record = json.loads(raw_lines[0])
        assert record["finding_id"] == "f-cross-1"
        assert record["rule"] == "unused-variable"
        assert record["confidence"] == 0.75
        assert record["quick_win"] is False
        assert record["safe_to_autofix"] is False
        assert "discovered_at" in record

    def test_app_finding_reads_engine_written_findings(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        findings_file = state_manager.get_findings_file(repo_name)
        engine_finding = _make_engine_finding(finding_id="f-app-read")
        append_findings(findings_file, [engine_finding])

        app_findings = state_manager.load_findings(repo_name)
        assert len(app_findings) == 1
        assert app_findings[0].finding_id == "f-app-read"
        assert app_findings[0].rule == "unused-import"
        assert app_findings[0].confidence == 0.9


class TestCorruptedEngineStateRecovery:
    """Garbage in state.json → engine.load_state() returns defaults, no crash."""

    def test_corrupted_state_returns_defaults(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("{this is not valid json !!!")

        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0
        assert state["created"] == []
        assert state["finding_activity"] == {}
        assert state["reconciliation_events"] == []

    def test_empty_state_file_returns_defaults(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("")

        state = load_state(state_file)
        assert state["open_issues"] == 0
        assert isinstance(state, dict)

    def test_non_dict_state_returns_defaults(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('"just a string"')

        state = load_state(state_file)
        assert isinstance(state, dict)
        assert state["open_issues"] == 0

    def test_partial_dict_state_fills_defaults(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text('{"open_issues": 7}')

        state = load_state(state_file)
        assert state["open_issues"] == 7
        assert state["open_prs"] == 0
        assert state["created"] == []
        assert state["finding_activity"] == {}
        assert state["reconciliation_events"] == []

    def test_recovered_state_can_be_saved_and_reloaded(self, registry, state_manager):
        _register_repo(registry)
        repo_name = "chain-repo"

        state_file = state_manager.get_state_file(repo_name)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text("GARBAGE DATA {{{")

        state = load_state(state_file)
        state["open_issues"] = 12
        state["created"] = ["pr-recovery-1"]
        save_state(state_file, state)

        reloaded = load_state(state_file)
        assert reloaded["open_issues"] == 12
        assert reloaded["created"] == ["pr-recovery-1"]
        assert reloaded["open_prs"] == 0

        app_state = state_manager.load_state(repo_name)
        assert app_state["open_issues"] == 12
        assert app_state["created"] == ["pr-recovery-1"]
