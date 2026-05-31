#!/usr/bin/env python3
"""Pytest-native tests for State Manager."""

import json
from pathlib import Path

import pytest

from bluei.app.models import Run
from bluei.app.state import StateManager


@pytest.fixture
def state_manager(tmp_path):
    return StateManager(tmp_path / "repos")


def test_append_findings(state_manager, make_app_finding):
    count = state_manager.append_findings("test-repo", [make_app_finding()])
    assert count == 1
    loaded = state_manager.load_findings("test-repo")
    assert len(loaded) == 1


def test_no_duplicate_findings(state_manager, make_app_finding):
    findings = [make_app_finding()]
    state_manager.append_findings("test-repo", findings)
    count = state_manager.append_findings("test-repo", findings)
    assert count == 0


def test_clear_findings(state_manager, make_app_finding):
    state_manager.append_findings("test-repo", [make_app_finding()])
    state_manager.clear_findings("test-repo")
    assert len(state_manager.load_findings("test-repo")) == 0


def test_save_load_issues(state_manager):
    issues = {"issues": [{"id": "QA-001", "title": "Test issue"}]}
    state_manager.save_issues("test-repo", issues)
    assert state_manager.load_issues("test-repo") == issues


def test_save_load_state(state_manager):
    state = {
        "open_issues": 5,
        "open_prs": 2,
        "created": ["item1"],
        "finding_activity": {},
    }
    state_manager.save_state("test-repo", state)
    loaded = state_manager.load_state("test-repo")
    assert loaded["open_issues"] == 5
    assert loaded["open_prs"] == 2


def test_save_load_run(state_manager):
    run = Run(
        id="run-001",
        repo_id="test-repo",
        phase="orchestrated",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:10:00Z",
        duration_seconds=600,
        status="completed",
    )
    state_manager.save_run("test-repo", run)
    loaded = state_manager.load_run("test-repo", "run-001")
    assert loaded is not None
    assert loaded.id == "run-001"
    assert loaded.status == "completed"


def test_list_runs(state_manager):
    for i in range(3):
        run = Run(
            id=f"run-{i:03d}",
            repo_id="test-repo",
            phase="issue-cycle",
            started_at=f"2026-01-0{i}T00:00:00Z",
            status="completed",
        )
        state_manager.save_run("test-repo", run)
    runs = state_manager.list_runs("test-repo", limit=2)
    assert len(runs) == 2


def test_save_load_baseline(state_manager):
    baseline = {
        "id": "baseline-001",
        "repo_id": "test-repo",
        "captured_at": "2026-01-01T00:00:00Z",
        "findings_total": 10,
        "health_score": 75.0,
    }
    state_manager.save_baseline("test-repo", baseline)
    loaded = state_manager.load_baseline("test-repo", "baseline-001")
    assert loaded is not None
    assert loaded["id"] == "baseline-001"
    assert loaded["health_score"] == 75.0


def test_list_baselines(state_manager):
    for i in range(2):
        baseline = {
            "id": f"baseline-{i:03d}",
            "repo_id": "test-repo",
            "findings_total": i * 5,
        }
        state_manager.save_baseline("test-repo", baseline)
    baselines = state_manager.list_baselines("test-repo")
    assert len(baselines) == 2


def test_review_state_round_trip(state_manager):
    active = {"prs": {"12": {"pr_number": 12, "status": "pending_review"}}}
    review = {
        "prs": {"12": {"last_snapshot_fingerprint": "abc", "retry_eligible": True}}
    }
    state_manager.save_active_prs("test-repo", active)
    state_manager.save_review_state("test-repo", review)

    loaded_active = state_manager.load_active_prs("test-repo")
    loaded_review = state_manager.load_review_state("test-repo")
    assert loaded_active["prs"]["12"]["pr_number"] == 12
    assert loaded_review["prs"]["12"]["retry_eligible"] is True


def test_append_review_event(state_manager):
    state_manager.append_review_event(
        "test-repo", {"event": "review_feedback_detected", "pr_number": 12}
    )
    path = state_manager.get_review_events_file("test-repo")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert "review_feedback_detected" in lines[0]


def test_atomic_json_write_leaves_no_tmp_files(state_manager):
    """Verify that save methods do not leave stray .tmp files behind."""
    issues = {"issues": [{"id": "QA-001", "title": "Test issue"}]}
    state_manager.save_issues("test-repo", issues)

    state = {"open_issues": 5, "open_prs": 2, "created": [], "finding_activity": {}}
    state_manager.save_state("test-repo", state)

    state_manager.save_active_prs("test-repo", {"prs": {"1": {"pr_number": 1}}})
    state_manager.save_review_state("test-repo", {"prs": {}})

    # Collect all state dirs that may have been created
    repo_dir = state_manager._get_repo_dir("test-repo")
    tmp_files = list(repo_dir.rglob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files found: {tmp_files}"


# --- Phase B2: Autonomous-review state surfaces ---


def test_save_load_review_run(state_manager):
    run_data = {
        "run_id": "rr-20260329-001",
        "repo": "test-repo",
        "pr_number": 42,
        "started_at": "2026-03-29T00:00:00Z",
        "status": "running",
    }
    path = state_manager.save_review_run("test-repo", run_data)
    loaded = state_manager.load_review_run("test-repo", "rr-20260329-001")
    assert loaded is not None
    assert loaded["run_id"] == "rr-20260329-001"
    assert loaded["pr_number"] == 42
    assert loaded["status"] == "running"
    # versioned defaults are applied
    assert loaded["version"] == 1
    assert loaded["findings_count"] == 0
    assert loaded["publish_status"] == "none"


def test_list_review_runs(state_manager):
    for i in range(3):
        state_manager.save_review_run(
            "test-repo",
            {
                "run_id": f"rr-{i:03d}",
                "repo": "test-repo",
                "status": "completed",
                "started_at": f"2026-01-01T00:00:0{i}Z",
            },
        )
    runs = state_manager.list_review_runs("test-repo", limit=2)
    assert len(runs) == 2
    # newest first
    assert runs[0]["run_id"] == "rr-002"


def test_load_nonexistent_review_run(state_manager):
    assert state_manager.load_review_run("test-repo", "does-not-exist") is None


def test_save_load_review_finding(state_manager):
    finding_data = {
        "finding_id": "rf-abc123",
        "repo": "test-repo",
        "path": "src/foo.py",
        "line": 10,
        "rule": "unused-import",
        "snippet": "import os  # never used",
        "confidence": 0.95,
        "severity": "low",
    }
    path = state_manager.save_review_finding("test-repo", "rf-abc123", finding_data)
    loaded = state_manager.load_review_finding("test-repo", "rf-abc123")
    assert loaded is not None
    assert loaded["finding_id"] == "rf-abc123"
    assert loaded["rule"] == "unused-import"
    assert loaded["version"] == 1
    assert "saved_at" in loaded


def test_load_nonexistent_review_finding(state_manager):
    assert state_manager.load_review_finding("test-repo", "does-not-exist") is None


def test_append_review_findings_dedupe(state_manager):
    findings = [
        {"finding_id": "rf-001", "repo": "test-repo", "rule": "a"},
        {"finding_id": "rf-002", "repo": "test-repo", "rule": "b"},
    ]
    written = state_manager.append_review_findings("test-repo", findings)
    assert written == 2

    # append same IDs again — should be deduplicated
    written2 = state_manager.append_review_findings("test-repo", findings)
    assert written2 == 0

    all_findings = state_manager.load_review_findings("test-repo")
    assert len(all_findings) == 2


def test_load_review_findings_empty(state_manager):
    assert state_manager.load_review_findings("nonexistent-repo") == []


def test_append_feedback_event(state_manager):
    event = {
        "source": "github_review_comment",
        "pr_number": 12,
        "finding_id": "rf-001",
        "signal": "positive",
        "payload": {"body": "LGTM"},
    }
    state_manager.append_feedback_event("test-repo", event)
    events = state_manager.load_feedback_events("test-repo")
    assert len(events) == 1
    assert events[0]["source"] == "github_review_comment"
    assert events[0]["signal"] == "positive"
    assert events[0]["version"] == 1
    assert events[0]["timestamp"] is not None


def test_load_feedback_events_empty(state_manager):
    assert state_manager.load_feedback_events("nonexistent-repo") == []


def test_save_load_learned_rules(state_manager):
    rules_data = {
        "rules": [
            {"id": "rule-001", "pattern": "TODO without author", "status": "active"},
            {"id": "rule-002", "pattern": "print() left in", "status": "tentative"},
        ],
        "active_count": 1,
        "tentative_count": 1,
    }
    state_manager.save_learned_rules("test-repo", rules_data)
    loaded = state_manager.load_learned_rules("test-repo")
    assert len(loaded["rules"]) == 2
    assert loaded["active_count"] == 1
    assert loaded["tentative_count"] == 1
    assert loaded["version"] == 1


def test_learned_rules_default_when_missing(state_manager):
    loaded = state_manager.load_learned_rules("brand-new-repo")
    assert loaded["version"] == 1
    assert loaded["rules"] == []
    assert loaded["active_count"] == 0


def test_save_load_review_publish_state(state_manager):
    pub_state = {
        "findings": {
            "rf-001": {"status": "published", "published_at": "2026-03-29T01:00:00Z"},
            "rf-002": {"status": "pending"},
        },
        "runs": {
            "rr-001": {"status": "published"},
        },
    }
    state_manager.save_review_publish_state("test-repo", pub_state)
    loaded = state_manager.load_review_publish_state("test-repo")
    assert "rf-001" in loaded["findings"]
    assert loaded["findings"]["rf-001"]["status"] == "published"
    assert loaded["version"] == 1


def test_review_publish_state_default_when_missing(state_manager):
    loaded = state_manager.load_review_publish_state("nonexistent-repo")
    assert loaded["version"] == 1
    assert loaded["findings"] == {}
    assert loaded["runs"] == {}


def test_review_runs_no_tmp_files(state_manager):
    """Phase B2 surfaces should not leave stray .tmp files."""
    state_manager.save_review_run(
        "test-repo", {"run_id": "rr-tmp-test", "repo": "test-repo"}
    )
    state_manager.save_learned_rules("test-repo", {"rules": []})
    state_manager.save_review_publish_state("test-repo", {"findings": {}})
    repo_dir = state_manager._get_repo_dir("test-repo")
    tmp_files = list(repo_dir.rglob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"


def test_review_findings_separate_from_issue_findings(state_manager, make_app_finding):
    """Review findings (rf-*) and issue findings (f-*) must not share storage."""
    # issue finding (old surface)
    state_manager.append_findings(
        "test-repo", [make_app_finding(finding_id="f-old", rule="old-rule")]
    )
    # review finding (new surface)
    state_manager.append_review_findings(
        "test-repo",
        [{"finding_id": "rf-new", "repo": "test-repo", "rule": "review-rule"}],
    )

    issue_findings = state_manager.load_findings("test-repo")
    review_findings = state_manager.load_review_findings("test-repo")

    issue_ids = {f.finding_id for f in issue_findings}
    review_ids = {f["finding_id"] for f in review_findings}

    assert "f-old" in issue_ids
    assert "rf-new" not in issue_ids
    assert "rf-new" in review_ids
    assert "f-old" not in review_ids


# --- Shallow-copy / nested-default leak regression tests ---


def test_review_publish_state_deep_copy_isolation(state_manager):
    """Mutating a loaded-default publish state must not leak into module-level default."""
    from bluei.app.state import DEFAULT_REVIEW_PUBLISH_STATE

    # Load two repos that both have no file on disk
    state_a = state_manager.load_review_publish_state("repo-a")
    state_b = state_manager.load_review_publish_state("repo-b")

    # Mutate state_a's nested structures
    state_a["findings"]["leaked-finding"] = {"status": "published"}
    state_a["runs"]["leaked-run"] = {"status": "published"}

    # DEFAULT must be pristine
    assert DEFAULT_REVIEW_PUBLISH_STATE["findings"] == {}, (
        "Module-level DEFAULT_REVIEW_PUBLISH_STATE was mutated!"
    )
    assert DEFAULT_REVIEW_PUBLISH_STATE["runs"] == {}, (
        "Module-level DEFAULT_REVIEW_PUBLISH_STATE was mutated!"
    )

    # state_b must also be pristine (not contaminated by state_a's mutation)
    assert state_b["findings"] == {}
    assert state_b["runs"] == {}


def test_active_prs_deep_copy_isolation(state_manager):
    """Mutating a loaded-default active_prs state must not affect module-level default."""
    from bluei.app.state import DEFAULT_ACTIVE_PRS_STATE

    prs_a = state_manager.load_active_prs("repo-a")
    prs_b = state_manager.load_active_prs("repo-b")

    prs_a["prs"]["123"] = {"pr_number": 123, "status": "tracking"}

    assert DEFAULT_ACTIVE_PRS_STATE["prs"] == {}
    assert prs_b["prs"] == {}


def test_review_state_deep_copy_isolation(state_manager):
    """Mutating a loaded-default review_state must not affect module-level default."""
    from bluei.app.state import DEFAULT_REVIEW_STATE

    rev_a = state_manager.load_review_state("repo-a")
    rev_b = state_manager.load_review_state("repo-b")

    rev_a["prs"]["999"] = {"pr_number": 999, "retry_eligible": True}

    assert DEFAULT_REVIEW_STATE["prs"] == {}
    assert rev_b["prs"] == {}


def test_learned_rules_deep_copy_isolation(state_manager):
    """Mutating a loaded-default learned_rules must not affect module-level default."""
    from bluei.app.state import DEFAULT_LEARNED_RULES

    rules_a = state_manager.load_learned_rules("repo-a")
    rules_b = state_manager.load_learned_rules("repo-b")

    rules_a["rules"].append(
        {"id": "injected-rule", "pattern": "TEST", "status": "active"}
    )

    assert DEFAULT_LEARNED_RULES["rules"] == []
    assert rules_b["rules"] == []


# ── Tests for bluei.engine.state functions ──


class TestRepairState:
    """repair_state() — state file recovery."""

    def test_repair_corrupted_json(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        state_path.write_text("{corrupted: json[, broken}")
        repaired = repair_state(state_path)
        assert repaired is True
        state = load_state(state_path)
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0
        assert state["created"] == []
        assert state["finding_activity"] == {}

    def test_repair_empty_file(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        state_path.write_text("")
        repaired = repair_state(state_path)
        assert repaired is True
        state = load_state(state_path)
        assert state["open_issues"] == 0

    def test_repair_whitespace_only(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        state_path.write_text("   \n  \n  ")
        repaired = repair_state(state_path)
        assert repaired is True
        state = load_state(state_path)
        assert state["open_issues"] == 0

    def test_repair_not_dict(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        state_path.write_text('"just a string"')
        repaired = repair_state(state_path)
        assert repaired is True
        state = load_state(state_path)
        assert isinstance(state, dict)

    def test_repair_not_dict_list(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        state_path.write_text("[1, 2, 3]")
        repaired = repair_state(state_path)
        assert repaired is True
        state = load_state(state_path)
        assert isinstance(state, dict)

    def test_repair_clean_returns_false(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        clean = {
            "open_issues": 5,
            "open_prs": 3,
            "created": [],
            "finding_activity": {},
            "reconciliation_events": [],
        }
        import json

        state_path.write_text(json.dumps(clean))
        repaired = repair_state(state_path)
        assert repaired is False

    def test_repair_missing_file_creates_default(self, tmp_path: Path) -> None:
        from bluei.engine.state import repair_state, load_state

        state_path = tmp_path / "state.json"
        assert not state_path.exists()
        repaired = repair_state(state_path)
        assert repaired is True
        assert state_path.exists()
        state = load_state(state_path)
        assert state["open_issues"] == 0


class TestGetEffectiveCooldown:
    """Exponential backoff cooldown formula."""

    def test_no_failure_uses_base(self) -> None:
        from bluei.engine.state import get_effective_cooldown

        state = {"finding_activity": {}}
        result = get_effective_cooldown("finding-1", state, 3600)
        assert result == 3600

    def test_single_failure_doubles(self) -> None:
        from bluei.engine.state import get_effective_cooldown

        state = {"finding_activity": {"finding-1": {"failure_count": 1}}}
        result = get_effective_cooldown("finding-1", state, 3600)
        assert result == 7200

    def test_two_failures_quadruples(self) -> None:
        from bluei.engine.state import get_effective_cooldown

        state = {"finding_activity": {"finding-1": {"failure_count": 2}}}
        result = get_effective_cooldown("finding-1", state, 3600)
        assert result == 14400

    def test_capped_at_max(self) -> None:
        from bluei.engine.state import get_effective_cooldown

        # MAX_COOLDOWN_SECONDS = 7 * 24 * 60 * 60 = 604800
        state = {"finding_activity": {"finding-1": {"failure_count": 10}}}
        result = get_effective_cooldown("finding-1", state, 3600)
        # 3600 * 2^10 = 3,686,400 > 604,800 -> capped
        assert result == 604800

    def test_unknown_finding_uses_base(self) -> None:
        from bluei.engine.state import get_effective_cooldown

        state = {"finding_activity": {"other-finding": {"failure_count": 5}}}
        result = get_effective_cooldown("unknown-finding", state, 7200)
        assert result == 7200

    def test_missing_activity_key(self) -> None:
        from bluei.engine.state import get_effective_cooldown

        state = {}
        result = get_effective_cooldown("finding-1", state, 1800)
        assert result == 1800


class TestIncrementFixAttempt:
    """fix_attempts increments correctly."""

    def test_increments_from_zero(self, tmp_path: Path) -> None:
        from bluei.engine.state import increment_fix_attempt, load_finding_record

        findings_file = tmp_path / "findings.jsonl"
        findings_file.write_text('{"finding_id": "f1"}\n')
        increment_fix_attempt("f1", findings_file, error=None)
        record = load_finding_record("f1", findings_file)
        assert record is not None
        assert record["fix_attempts"] == 1
        assert "last_fix_at" in record
        assert "last_fix_error" not in record

    def test_increments_existing_count(self, tmp_path: Path) -> None:
        from bluei.engine.state import increment_fix_attempt, load_finding_record

        findings_file = tmp_path / "findings.jsonl"
        findings_file.write_text('{"finding_id": "f1", "fix_attempts": 3}\n')
        increment_fix_attempt("f1", findings_file, error=None)
        record = load_finding_record("f1", findings_file)
        assert record["fix_attempts"] == 4

    def test_stores_error(self, tmp_path: Path) -> None:
        from bluei.engine.state import increment_fix_attempt, load_finding_record

        findings_file = tmp_path / "findings.jsonl"
        findings_file.write_text('{"finding_id": "f1"}\n')
        increment_fix_attempt("f1", findings_file, error="merge conflict")
        record = load_finding_record("f1", findings_file)
        assert record["last_fix_error"] == "merge conflict"

    def test_truncates_long_error(self, tmp_path: Path) -> None:
        from bluei.engine.state import increment_fix_attempt, load_finding_record

        findings_file = tmp_path / "findings.jsonl"
        findings_file.write_text('{"finding_id": "f1"}\n')
        long_error = "x" * 1000
        increment_fix_attempt("f1", findings_file, error=long_error)
        record = load_finding_record("f1", findings_file)
        assert len(record["last_fix_error"]) == 500

    def test_handles_missing_file_gracefully(self, tmp_path: Path) -> None:
        from bluei.engine.state import increment_fix_attempt

        findings_file = tmp_path / "missing.jsonl"
        increment_fix_attempt("f1", findings_file, error=None)

    def test_handles_missing_finding_gracefully(self, tmp_path: Path) -> None:
        from bluei.engine.state import increment_fix_attempt

        findings_file = tmp_path / "findings.jsonl"
        findings_file.write_text('{"finding_id": "other"}\n')
        increment_fix_attempt("f1", findings_file, error=None)
        from bluei.engine.state import load_finding_record

        record = load_finding_record("other", findings_file)
        assert "fix_attempts" not in record


def test_jsonl_rotation_by_size(state_manager, tmp_path):
    from unittest.mock import patch

    jsonl_path = state_manager.get_review_events_file("test-repo")
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    big_line = json.dumps({"event": "x", "data": "a" * 200}) + "\n"
    with open(jsonl_path, "w") as f:
        for _ in range(10):
            f.write(big_line)

    assert jsonl_path.exists()

    with patch("bluei.app.state._EVENT_LOG_MAX_BYTES", 1):
        state_manager.append_review_event("test-repo", {"event": "trigger"})

    bak_path = jsonl_path.with_suffix(".jsonl.bak")
    assert bak_path.exists(), ".bak file should exist after rotation"
    assert (
        not jsonl_path.exists() or jsonl_path.stat().st_size < bak_path.stat().st_size
    )

    state_manager.append_review_event("test-repo", {"event": "after_rotation"})
    new_jsonl = state_manager.get_review_events_file("test-repo")
    assert new_jsonl.exists()
    lines = new_jsonl.read_text().strip().splitlines()
    assert len(lines) >= 1
    assert "after_rotation" in lines[0]


def test_jsonl_rotation_no_name_error(state_manager):
    from bluei.app.state import _rotate_jsonl_if_needed, _EVENT_LOG_MAX_BYTES
    from unittest.mock import patch

    jsonl_path = state_manager.get_review_events_file("test-repo")
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)

    big_line = json.dumps({"event": "x", "data": "a" * 200}) + "\n"
    with open(jsonl_path, "w") as f:
        for _ in range(10):
            f.write(big_line)

    with patch("bluei.app.state._EVENT_LOG_MAX_BYTES", 1):
        _rotate_jsonl_if_needed(jsonl_path)

    bak_path = jsonl_path.with_suffix(".jsonl.bak")
    assert bak_path.exists()
    assert not jsonl_path.exists()


class TestEngineLoadStateCorruption:
    """load_state() self-heals from corrupted JSON files."""

    def test_corrupted_json_returns_defaults(self, tmp_path: Path) -> None:
        from bluei.engine.state import load_state

        state_path = tmp_path / "state.json"
        state_path.write_text("{corrupted: json[, broken}")
        state = load_state(state_path)
        assert state == {
            "open_issues": 0,
            "open_prs": 0,
            "created": [],
            "finding_activity": {},
            "reconciliation_events": [],
        }

    def test_truncated_json_returns_defaults(self, tmp_path: Path) -> None:
        from bluei.engine.state import load_state

        state_path = tmp_path / "state.json"
        state_path.write_text('{"open_issues": 5, "open_prs')
        state = load_state(state_path)
        assert state["open_issues"] == 0

    def test_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        from bluei.engine.state import load_state

        state_path = tmp_path / "state.json"
        state_path.write_text("")
        state = load_state(state_path)
        assert state["open_issues"] == 0


class TestEngineSaveStateAtomic:
    """save_state() writes atomically (temp + rename)."""

    def test_round_trip(self, tmp_path: Path) -> None:
        from bluei.engine.state import load_state, save_state

        state_path = tmp_path / "state.json"
        original = {
            "open_issues": 42,
            "open_prs": 7,
            "created": ["pr-1", "pr-2"],
            "finding_activity": {"f1": {"last_action": "fix"}},
            "reconciliation_events": [],
        }
        save_state(state_path, original)
        loaded = load_state(state_path)
        assert loaded["open_issues"] == 42
        assert loaded["open_prs"] == 7
        assert loaded["created"] == ["pr-1", "pr-2"]

    def test_no_tmp_file_lingers(self, tmp_path: Path) -> None:
        from bluei.engine.state import save_state

        state_path = tmp_path / "state.json"
        save_state(state_path, {"open_issues": 1})
        tmp_files = list(tmp_path.glob(".state-*.tmp"))
        assert tmp_files == [], f"Unexpected temp files: {tmp_files}"

    def test_overwrite_preserves_data(self, tmp_path: Path) -> None:
        from bluei.engine.state import load_state, save_state

        state_path = tmp_path / "state.json"
        save_state(state_path, {"open_issues": 1})
        save_state(state_path, {"open_issues": 99})
        loaded = load_state(state_path)
        assert loaded["open_issues"] == 99


# ── Tests extracted from test_phase5b_modules_remaining.py ──

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from bluei.engine.models import Finding, now_iso
from bluei.engine.reforge import RefactorPhase, RefactorWork
from bluei.engine.state import (
    append_findings,
    count_actionable_issues,
    filter_findings_by_cooldown,
    get_effective_cooldown,
    get_pending_refactor_work,
    guard_open_issues,
    guard_open_prs,
    increment_fix_attempt,
    load_batches,
    load_findings_seen,
    load_finding_record,
    load_issues,
    load_refactor_work,
    load_state,
    mark_finding_activity,
    reconcile_open_workload,
    record_reconciliation_event,
    repair_state,
    save_batch_record,
    save_issues,
    save_refactor_work,
    save_state,
    update_batch_record,
    update_finding_record,
)


def _engine_finding(
    finding_id="f-1",
    repo="demo",
    path="src/foo.py",
    rule="test-rule",
    snippet="bad code",
    line=10,
    confidence=0.9,
    quick_win=True,
    safe_to_autofix=True,
):
    return Finding(
        finding_id=finding_id,
        repo=repo,
        path=path,
        line=line,
        rule=rule,
        snippet=snippet,
        confidence=confidence,
        quick_win=quick_win,
        safe_to_autofix=safe_to_autofix,
    )


class TestLoadFindingsSeen:
    """load_findings_seen reads finding IDs from JSONL."""

    def test_nonexistent_returns_empty(self, tmp_path):
        assert load_findings_seen(tmp_path / "nope.jsonl") == set()

    def test_loads_finding_ids(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\n{"finding_id": "f2"}\n')
        result = load_findings_seen(f)
        assert result == {"f1", "f2"}

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\nbad\n{"finding_id": "f2"}\n')
        result = load_findings_seen(f)
        assert result == {"f1", "f2"}

    def test_skips_lines_without_finding_id(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"other": 1}\n{"finding_id": "f1"}\n')
        result = load_findings_seen(f)
        assert result == {"f1"}

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\n\n  \n{"finding_id": "f2"}\n')
        result = load_findings_seen(f)
        assert result == {"f1", "f2"}


class TestAppendFindingsEngine:
    """engine.state.append_findings writes findings to JSONL."""

    def test_appends_new_finding(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        finding = _engine_finding(finding_id="f1")
        count = append_findings(f, [finding])
        assert count == 1
        data = json.loads(f.read_text().strip())
        assert data["finding_id"] == "f1"
        assert "discovered_at" in data

    def test_skips_duplicate(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        finding = _engine_finding(finding_id="f1")
        append_findings(f, [finding])
        count = append_findings(f, [finding])
        assert count == 0

    def test_mixed_new_and_duplicate(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        append_findings(f, [_engine_finding(finding_id="f1")])
        count = append_findings(
            f, [_engine_finding(finding_id="f1"), _engine_finding(finding_id="f2")]
        )
        assert count == 1


class TestLoadIssues:
    """load_issues from JSON with validation."""

    def test_nonexistent_returns_empty(self, tmp_path):
        result = load_issues(tmp_path / "nope.json")
        assert result == {"issues": []}

    def test_not_dict_returns_empty(self, tmp_path):
        f = tmp_path / "issues.json"
        f.write_text("[1, 2, 3]")
        result = load_issues(f)
        assert result == {"issues": []}

    def test_missing_issues_key_adds_empty(self, tmp_path):
        f = tmp_path / "issues.json"
        f.write_text('{"other": 1}')
        result = load_issues(f)
        assert result["issues"] == []

    def test_issues_not_list_fixes(self, tmp_path):
        f = tmp_path / "issues.json"
        f.write_text('{"issues": "not a list"}')
        result = load_issues(f)
        assert result["issues"] == []


class TestSaveIssues:
    """save_issues writes JSON with issues."""

    def test_creates_file(self, tmp_path):
        f = tmp_path / "sub" / "issues.json"
        save_issues(f, {"issues": [{"id": 1}]})
        data = json.loads(f.read_text())
        assert data["issues"] == [{"id": 1}]


class TestGuardOpenIssues:
    """guard_open_issues blocks at or above cap."""

    def test_blocks_at_cap(self):
        ok, msg = guard_open_issues(10, 10)
        assert ok is False
        assert "guard-block" in msg

    def test_blocks_above_cap(self):
        ok, msg = guard_open_issues(11, 10)
        assert ok is False

    def test_passes_below_cap(self):
        ok, msg = guard_open_issues(5, 10)
        assert ok is True
        assert "guard-pass" in msg


class TestGuardOpenPrs:
    """guard_open_prs blocks at or above cap."""

    def test_blocks_at_cap(self):
        ok, msg = guard_open_prs(5, 5)
        assert ok is False

    def test_passes_below_cap(self):
        ok, msg = guard_open_prs(3, 5)
        assert ok is True


class TestRecordReconciliationEvent:
    """record_reconciliation_event tracks reconciliation history."""

    def test_records_event_and_appends_log(self, tmp_path):
        state = {"reconciliation_events": []}
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        event = record_reconciliation_event(
            state,
            log_file,
            before_issues=5,
            before_prs=3,
            after_issues=4,
            after_prs=2,
            reason="test",
        )
        assert event["reason"] == "test"
        assert event["before"]["open_issues"] == 5
        assert event["after"]["open_prs"] == 2
        assert len(state["reconciliation_events"]) == 1

    def test_trims_events_above_max(self, tmp_path):
        state = {"reconciliation_events": []}
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        for i in range(110):
            record_reconciliation_event(
                state,
                log_file,
                before_issues=i,
                before_prs=0,
                after_issues=i,
                after_prs=0,
                reason=f"event-{i}",
            )
        assert len(state["reconciliation_events"]) == 100


class TestReconcileOpenWorkload:
    """reconcile_open_workload fetches live counts or simulates."""

    @patch("bluei.engine.state.fetch_github_live_counts")
    @patch("bluei.engine.state.get_origin_url")
    def test_github_live_counts(self, mock_origin, mock_live, tmp_path):
        mock_origin.return_value = "https://github.com/owner/repo"
        mock_live.return_value = ({"open_issues": 3, "open_prs": 2}, "github-live")
        state = {"open_issues": 0, "open_prs": 0}
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        issues, prs, event = reconcile_open_workload(
            tmp_path, state, log_file, None, None
        )
        assert issues == 3
        assert prs == 2
        assert state["open_issues"] == 3

    @patch("bluei.engine.state.fetch_github_live_counts")
    @patch("bluei.engine.state.get_origin_url")
    def test_github_live_none_with_github_url(self, mock_origin, mock_live, tmp_path):
        mock_origin.return_value = "https://github.com/owner/repo"
        mock_live.return_value = (None, "api-rate-limited")
        state = {"open_issues": 5, "open_prs": 1}
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        issues, prs, event = reconcile_open_workload(
            tmp_path, state, log_file, None, None
        )
        assert event["reason"] == "api-rate-limited"

    @patch("bluei.engine.state.fetch_github_live_counts")
    @patch("bluei.engine.state.get_origin_url")
    def test_non_github_simulates(self, mock_origin, mock_live, tmp_path):
        mock_origin.return_value = "https://gitlab.com/owner/repo"
        mock_live.return_value = (None, "not-github")
        state = {"open_issues": 0, "open_prs": 0}
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        issues, prs, event = reconcile_open_workload(
            tmp_path, state, log_file, simulate_open_issues=7, simulate_open_prs=4
        )
        assert issues == 7
        assert prs == 4
        assert event["reason"] == "cli-simulated-open-workload"


class TestMarkFindingActivity:
    """mark_finding_activity records actions on findings."""

    def test_empty_finding_ids_noop(self):
        state = {}
        mark_finding_activity(state, [], "test")
        assert state == {}

    def test_records_activity(self):
        state = {}
        mark_finding_activity(state, ["f1"], "fix-attempt")
        assert state["finding_activity"]["f1"]["last_action"] == "fix-attempt"
        assert "last_action_at" in state["finding_activity"]["f1"]

    def test_records_failure_count(self):
        state = {}
        mark_finding_activity(state, ["f1"], "fix-attempt", failure_count=3)
        assert state["finding_activity"]["f1"]["failure_count"] == 3

    def test_records_last_error(self):
        state = {}
        mark_finding_activity(state, ["f1"], "fix-attempt", last_error="merge conflict")
        assert state["finding_activity"]["f1"]["last_error"] == "merge conflict"

    def test_updates_existing_entry(self):
        state = {
            "finding_activity": {
                "f1": {"last_action": "old", "last_action_at": "old_ts"}
            }
        }
        mark_finding_activity(state, ["f1"], "new-action")
        assert state["finding_activity"]["f1"]["last_action"] == "new-action"

    def test_multiple_findings(self):
        state = {}
        mark_finding_activity(state, ["f1", "f2"], "fix-attempt")
        assert "f1" in state["finding_activity"]
        assert "f2" in state["finding_activity"]


class TestFilterFindingsByCooldown:
    """filter_findings_by_cooldown suppresses recently-active findings."""

    def test_no_activity_passes_all(self, tmp_path):
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        state = {}
        findings = [_engine_finding(finding_id="f1"), _engine_finding(finding_id="f2")]
        allowed, suppressed = filter_findings_by_cooldown(
            findings, state, 3600, log_file
        )
        assert len(allowed) == 2
        assert len(suppressed) == 0

    def test_recent_action_suppresses(self, tmp_path):
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        recent_ts = now_iso()
        state = {
            "finding_activity": {
                "f1": {"last_action_at": recent_ts, "last_action": "fix-attempt"}
            }
        }
        findings = [_engine_finding(finding_id="f1")]
        allowed, suppressed = filter_findings_by_cooldown(
            findings, state, 3600, log_file
        )
        assert len(allowed) == 0
        assert len(suppressed) == 1

    def test_expired_cooldown_passes(self, tmp_path):
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = {
            "finding_activity": {
                "f1": {"last_action_at": old_ts, "last_action": "fix-attempt"}
            }
        }
        findings = [_engine_finding(finding_id="f1")]
        allowed, suppressed = filter_findings_by_cooldown(
            findings, state, 3600, log_file
        )
        assert len(allowed) == 1
        assert len(suppressed) == 0

    def test_failure_count_increases_cooldown(self, tmp_path):
        log_file = tmp_path / "log.txt"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("")
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        state = {
            "finding_activity": {
                "f1": {
                    "last_action_at": old_ts,
                    "last_action": "fix-attempt",
                    "failure_count": 2,
                }
            }
        }
        findings = [_engine_finding(finding_id="f1")]
        allowed, suppressed = filter_findings_by_cooldown(
            findings, state, 3600, log_file
        )
        assert len(suppressed) == 1


class TestLoadFindingRecord:
    """load_finding_record retrieves a single record by ID."""

    def test_nonexistent_returns_none(self, tmp_path):
        assert load_finding_record("f1", tmp_path / "nope.jsonl") is None

    def test_loads_matching_record(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1", "rule": "test"}\n{"finding_id": "f2"}\n')
        result = load_finding_record("f1", f)
        assert result is not None
        assert result["rule"] == "test"

    def test_returns_none_for_missing_id(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f2"}\n')
        assert load_finding_record("f1", f) is None

    def test_skips_malformed(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('bad\n{"finding_id": "f1"}\n')
        result = load_finding_record("f1", f)
        assert result is not None


class TestUpdateFindingRecord:
    """update_finding_record modifies a single record in-place."""

    def test_nonexistent_returns_false(self, tmp_path):
        assert update_finding_record("f1", tmp_path / "nope.jsonl", {"x": 1}) is False

    def test_updates_matching(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1", "rule": "old"}\n')
        result = update_finding_record("f1", f, {"rule": "new"})
        assert result is True
        record = json.loads(f.read_text().strip())
        assert record["rule"] == "new"

    def test_preserves_malformed_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('bad line\n{"finding_id": "f1"}\n')
        update_finding_record("f1", f, {"x": 1})
        lines = f.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_returns_false_if_not_found(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f2"}\n')
        assert update_finding_record("f1", f, {"x": 1}) is False


class TestLoadRefactorWork:
    """load_refactor_work deserialises RefactorWork from a finding record."""

    def test_none_when_no_record(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        assert load_refactor_work("f1", f) is None

    def test_none_when_no_refactor_work_key(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\n')
        assert load_refactor_work("f1", f) is None

    def test_none_when_refactor_work_not_dict(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1", "refactor_work": "bad"}\n')
        assert load_refactor_work("f1", f) is None

    def test_loads_valid_refactor_work(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        rw = {
            "finding_id": "f1",
            "phase": "splitting",
            "planned_targets": ["part1.ts", "part2.ts"],
            "original_line_count": 500,
            "target_lines_per_file": 200,
            "written_files": ["part1.ts"],
            "baseline_fingerprint": "abc",
            "needs_human_review": False,
            "review_outcome": None,
        }
        f.write_text(json.dumps({"finding_id": "f1", "refactor_work": rw}) + "\n")
        result = load_refactor_work("f1", f)
        assert result is not None
        assert result.phase == RefactorPhase.SPLITTING
        assert result.original_line_count == 500
        assert "part1.ts" in result.written_files

    def test_handles_corrupt_refactor_work(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text(
            '{"finding_id": "f1", "refactor_work": {"phase": "INVALID_PHASE"}}\n'
        )
        result = load_refactor_work("f1", f)
        assert result is None


class TestSaveRefactorWork:
    """save_refactor_work serialises RefactorWork into a finding record."""

    def test_saves_and_returns_true(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\n')
        rw = RefactorWork(
            finding_id="f1",
            phase=RefactorPhase.PLANNING,
            planned_targets=["a.ts"],
            original_line_count=100,
            target_lines_per_file=50,
            written_files={"a.ts"},
            baseline_fingerprint="fp",
        )
        result = save_refactor_work("f1", f, rw)
        assert result is True
        record = json.loads(f.read_text().strip())
        assert record["refactor_work"]["finding_id"] == "f1"
        assert record["refactor_phase"] == "planning"

    def test_returns_false_when_not_found(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f2"}\n')
        rw = RefactorWork(finding_id="f1")
        assert save_refactor_work("f1", f, rw) is False


class TestGetPendingRefactorWork:
    """get_pending_refactor_work lists only non-terminal phases."""

    def test_nonexistent_returns_empty(self, tmp_path):
        assert get_pending_refactor_work(tmp_path / "nope.jsonl") == []

    def test_returns_pending_only(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        pending = {"finding_id": "f1", "refactor_work": {"phase": "splitting"}}
        done = {"finding_id": "f2", "refactor_work": {"phase": "done"}}
        aborted = {"finding_id": "f3", "refactor_work": {"phase": "aborted"}}
        f.write_text(
            json.dumps(pending)
            + "\n"
            + json.dumps(done)
            + "\n"
            + json.dumps(aborted)
            + "\n"
        )
        result = get_pending_refactor_work(f)
        assert len(result) == 1
        assert result[0]["finding_id"] == "f1"

    def test_skips_non_dict_refactor_work(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1", "refactor_work": "not a dict"}\n')
        result = get_pending_refactor_work(f)
        assert result == []

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text(
            'bad\n{"finding_id": "f1", "refactor_work": {"phase": "planning"}}\n'
        )
        result = get_pending_refactor_work(f)
        assert len(result) == 1


class TestLoadBatches:
    """load_batches reads batch records from JSONL."""

    def test_nonexistent_returns_empty(self, tmp_path):
        assert load_batches(tmp_path / "nope.jsonl") == []

    def test_loads_valid(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('{"batch_id": "b1"}\n{"batch_id": "b2"}\n')
        result = load_batches(f)
        assert len(result) == 2

    def test_skips_malformed(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('bad\n{"batch_id": "b1"}\n')
        result = load_batches(f)
        assert len(result) == 1

    def test_skips_blank_lines(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('{"batch_id": "b1"}\n\n  \n')
        result = load_batches(f)
        assert len(result) == 1


class TestSaveBatchRecord:
    """save_batch_record appends to JSONL."""

    def test_appends_record(self, tmp_path):
        f = tmp_path / "sub" / "batches.jsonl"
        save_batch_record(f, {"batch_id": "b1", "status": "open"})
        record = json.loads(f.read_text().strip())
        assert record["batch_id"] == "b1"


class TestUpdateBatchRecord:
    """update_batch_record modifies a batch in-place."""

    def test_nonexistent_returns_false(self, tmp_path):
        assert update_batch_record(tmp_path / "nope.jsonl", "b1", {}) is False

    def test_updates_matching(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text(
            '{"batch_id": "b1", "status": "open"}\n{"batch_id": "b2", "status": "open"}\n'
        )
        result = update_batch_record(f, "b1", {"status": "closed"})
        assert result is True
        lines = f.read_text().strip().splitlines()
        records = [json.loads(l) for l in lines]
        b1 = [r for r in records if r["batch_id"] == "b1"][0]
        assert b1["status"] == "closed"

    def test_returns_false_when_not_found(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('{"batch_id": "b2"}\n')
        assert update_batch_record(f, "b1", {}) is False

    def test_skips_malformed_lines(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('bad\n{"batch_id": "b1"}\n')
        result = update_batch_record(f, "b1", {"status": "closed"})
        assert result is True


class TestSaveStateAtomicError:
    """save_state cleans up temp files on error."""

    def test_save_state_cleans_up_on_error(self, tmp_path):
        state_path = tmp_path / "state.json"
        with patch("os.replace", side_effect=OSError("nope")):
            with pytest.raises(OSError):
                save_state(state_path, {"open_issues": 1})
        tmp_files = list(tmp_path.glob(".state-*.tmp"))
        assert tmp_files == []


class TestBlankLineJsonlReaders:
    """All JSONL readers handle blank lines gracefully."""

    def test_load_finding_record_skips_blank_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\n\n  \n{"finding_id": "f2"}\n')
        result = load_finding_record("f2", f)
        assert result is not None
        assert result["finding_id"] == "f2"

    def test_update_finding_record_skips_blank_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text('{"finding_id": "f1"}\n\n  \n{"finding_id": "f2"}\n')
        result = update_finding_record("f2", f, {"x": 1})
        assert result is True

    def test_get_pending_refactor_skips_blank_lines(self, tmp_path):
        f = tmp_path / "findings.jsonl"
        f.write_text(
            '\n\n{"finding_id": "f1", "refactor_work": {"phase": "planning"}}\n'
        )
        result = get_pending_refactor_work(f)
        assert len(result) == 1

    def test_load_batches_skips_blank_lines(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('\n\n{"batch_id": "b1"}\n')
        result = load_batches(f)
        assert len(result) == 1

    def test_update_batch_record_skips_blank_lines(self, tmp_path):
        f = tmp_path / "batches.jsonl"
        f.write_text('\n\n{"batch_id": "b1"}\n')
        result = update_batch_record(f, "b1", {"status": "closed"})
        assert result is True


class TestLoadStateEdgeCases:
    """load_state handles various malformed inputs."""

    def test_missing_file_returns_defaults(self, tmp_path):
        state = load_state(tmp_path / "nope.json")
        assert state["open_issues"] == 0
        assert state["open_prs"] == 0
        assert state["created"] == []

    def test_corrupt_json_returns_defaults(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("{bad json")
        state = load_state(f)
        assert state["open_issues"] == 0

    def test_non_dict_sets_defaults(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("42")
        state = load_state(f)
        assert "open_issues" in state
        assert isinstance(state["created"], list)

    def test_partial_dict_fills_defaults(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text('{"open_issues": 5}')
        state = load_state(f)
        assert state["open_issues"] == 5
        assert state["open_prs"] == 0
        assert state["created"] == []
        assert state["finding_activity"] == {}
        assert state["reconciliation_events"] == []


class TestRepairStateEdgeCases:
    """repair_state handles non-JSON exceptions."""

    def test_general_exception_triggers_repair(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("some text")
        with patch("json.loads", side_effect=RuntimeError("boom")):
            repaired = repair_state(state_path)
        assert repaired is True
