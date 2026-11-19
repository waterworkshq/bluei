"""Acceptance-boundary tests for try_replay's ``record_success`` flag.

The mainline caller defers positive (HIT) pattern feedback to its own
acceptance boundary (scope/validation/closure/retained artifact).
``record_success=False`` must suppress ONLY the positive HIT recording;
MISS/FAILURE observations remain recorded per ``record_outcome`` (ADR-0004).

Transport/model-free: local tmp_path git repos, real on-disk
FixPatternStore, patched run_capture for baseline validation. No network,
no model, no gh.
"""

import ast
import json
import subprocess
from unittest.mock import patch

import pytest

from bluei.engine.pattern_replay import _clear_pattern_cache, try_replay
from bluei.engine.pattern_store import FixPattern, FixPatternStore, ReplayOutcome


def make_pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/example.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "",
        "confidence": 0.95,
        "success_count": 3,
        "failure_count": 0,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-05-17T00:00:00+00:00",
        "last_used_at": None,
        "last_verified_at": None,
        "source_finding_ids": ["finding-1"],
    }
    data.update(overrides)
    return FixPattern(**data)


@pytest.fixture(autouse=True)
def _fresh_pattern_cache():
    # Pattern ids are random per store (fp-<hex>); a module-level resolution
    # cache entry from another test's store would resolve to a pid that does
    # not exist in this test's store and silently no-op outcome recording.
    _clear_pattern_cache()
    yield
    _clear_pattern_cache()


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "repos" / "test-repo" / "state" / "fix_patterns.jsonl"


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "logs" / "test.log"


@pytest.fixture
def baseline_checks():
    return {"lint": ["python3", "-c", "pass"]}


def _seed_store(store_path):
    store = FixPatternStore(store_path)
    pid = store.append(make_pattern(confidence=0.95))
    store._rebuild_from_disk()
    return store, pid


def _last_record(store_path):
    return json.loads(store_path.read_text().strip().split("\n")[-1])


def _committed_target_repo(git_repo, git_commit_all):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)
    return target


def test_record_success_false_replays_but_does_not_record_hit(
    store_path, git_repo, git_commit_all, log_file, baseline_checks, make_finding
):
    target = _committed_target_repo(git_repo, git_commit_all)
    store, pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch(
        "bluei.engine.pattern_replay.run_capture", return_value=(0, "")
    ) as mock_rc:
        result = try_replay(
            git_repo,
            finding,
            store,
            baseline_checks,
            log_file,
            record_success=False,
        )

    # The replay itself is unaffected: applied, validated, HIT returned.
    # Whitespace-insensitive application check (structural rung normalizes
    # indentation): the bare except must now catch Exception, semantically.
    assert result == (True, pid)
    handlers = [
        node
        for node in ast.walk(ast.parse(target.read_text()))
        if isinstance(node, ast.ExceptHandler)
    ]
    assert handlers, "expected an except handler in the replayed file"
    assert all(
        isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
        for handler in handlers
    ), f"bare except not converted: {[ast.dump(h.type) for h in handlers]}"
    assert mock_rc.called  # validation actually ran

    # But no positive feedback reached the store.
    record = _last_record(store_path)
    assert record["success_count"] == 3
    assert record["skip_count"] == 0
    assert record["failure_count"] == 0
    assert record.get("last_verified_at") is None

    # Positive application log is evidence of the run, still emitted.
    assert "pattern-replay-success" in log_file.read_text()


def test_record_success_false_preserves_miss_on_file_not_found(
    store_path, git_repo, log_file, make_finding
):
    store, _pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/nonexistent.py", snippet="except:\n    pass"
    )

    result = try_replay(
        git_repo, finding, store, {}, log_file, record_success=False
    )

    assert result == (False, None)
    record = _last_record(store_path)
    assert record["skip_count"] == 1  # MISS still recorded
    assert record["success_count"] == 3
    assert record.get("last_used_at") is not None


def test_record_success_false_preserves_miss_on_snippet_not_found(
    store_path, git_repo, git_commit_all, log_file, make_finding
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    (src_dir / "example.py").write_text("def foo():\n    return 42\n")
    git_commit_all(git_repo)
    store, _pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(
        git_repo, finding, store, {}, log_file, record_success=False
    )

    assert result == (False, None)
    record = _last_record(store_path)
    assert record["skip_count"] == 1  # MISS still recorded
    assert record["success_count"] == 3


def test_record_success_false_preserves_failure_on_validation_failure(
    store_path,
    git_repo,
    git_commit_all,
    log_file,
    baseline_checks,
    make_finding,
):
    _committed_target_repo(git_repo, git_commit_all)
    store, pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    def mock_run_capture(cmd, cwd=None, timeout=0):
        if cmd == ["git", "checkout", "--", "src/example.py"]:
            subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=10)
            return (0, "")
        return (1, "check failed")

    with patch(
        "bluei.engine.pattern_replay.run_capture", side_effect=mock_run_capture
    ):
        result = try_replay(
            git_repo,
            finding,
            store,
            baseline_checks,
            log_file,
            record_success=False,
        )

    assert result == (False, None)
    record = _last_record(store_path)
    assert record["failure_count"] == 1  # FAILURE still recorded
    assert record["success_count"] == 3
    assert record.get("last_failed_at") is not None


def test_record_success_false_with_record_outcome_false_records_nothing(
    store_path,
    git_repo,
    git_commit_all,
    log_file,
    baseline_checks,
    make_finding,
):
    _committed_target_repo(git_repo, git_commit_all)
    store, pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        result = try_replay(
            git_repo,
            finding,
            store,
            baseline_checks,
            log_file,
            record_outcome=False,
            record_success=False,
        )

    assert result == (True, pid)
    record = _last_record(store_path)
    assert record["success_count"] == 3
    assert record["skip_count"] == 0
    assert record["failure_count"] == 0


@pytest.mark.parametrize(
    "call_kwargs",
    [{}, {"record_success": True}],
    ids=["pure-default", "explicit-true"],
)
def test_default_behavior_unchanged_records_hit(
    store_path,
    git_repo,
    git_commit_all,
    log_file,
    baseline_checks,
    make_finding,
    call_kwargs,
):
    _committed_target_repo(git_repo, git_commit_all)
    store, pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        result = try_replay(
            git_repo, finding, store, baseline_checks, log_file, **call_kwargs
        )

    assert result == (True, pid)
    record = _last_record(store_path)
    assert record["success_count"] == 4  # HIT recorded as before
    assert record.get("last_verified_at") is not None


def test_caller_records_deferred_hit_after_acceptance(
    store_path, git_repo, git_commit_all, log_file, baseline_checks, make_finding
):
    """Parent contract: after outer acceptance, the caller records the HIT
    itself with the public store method — exactly once."""
    _committed_target_repo(git_repo, git_commit_all)
    store, pid = _seed_store(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        replayed, replay_pid = try_replay(
            git_repo,
            finding,
            store,
            baseline_checks,
            log_file,
            record_success=False,
        )

    assert (replayed, replay_pid) == (True, pid)

    # Outer acceptance passed (scope/validation/closure/retained artifact):
    store.record_replay_outcome(pid, ReplayOutcome.HIT)

    record = _last_record(store_path)
    assert record["success_count"] == 4
    assert record["skip_count"] == 0
    assert record["failure_count"] == 0
    assert record.get("last_verified_at") is not None
