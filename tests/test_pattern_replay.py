import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.pattern_replay import (
    AUTO_REPLAY_THRESHOLD,
    PROMPT_HINT_THRESHOLD,
    _find_snippet_in_file,
    try_replay,
)
from bluei.engine.pattern_store import (
    FixPattern,
    FixPatternStore,
)


def make_pattern(**overrides):
    data = {
        "pattern_id": "",
        "rule": "broad-except",
        "language": "python",
        "file_path": "src/example.py",
        "before_snippet": "except:\n    pass",
        "after_snippet": "except Exception:\n    pass",
        "diff_patch": "--- a/src/example.py\n+++ b/src/example.py\n-except:\n+except Exception:\n",
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


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "repos" / "test-repo" / "state" / "fix_patterns.jsonl"


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "logs" / "test.log"


@pytest.fixture
def baseline_checks():
    return {"lint": ["python3", "-c", "pass"]}


def _store_pattern_with_confidence(store, confidence):
    pid = store.append(make_pattern())
    store.update_confidence(pid, confidence - 0.5)
    store._rebuild_from_disk()
    return pid


def test_replay_no_matching_pattern_returns_false_none(
    store_path, git_repo, log_file, make_finding
):
    store = FixPatternStore(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_replay_no_matching_pattern_logs_miss(
    store_path, git_repo, log_file, make_finding
):
    store = FixPatternStore(store_path)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    try_replay(git_repo, finding, store, {}, log_file)

    log_content = log_file.read_text()
    assert "pattern-replay-miss" in log_content
    assert "reason=no_matching_pattern" in log_content


def test_replay_deactivated_pattern_returns_false_none(
    store_path, git_repo, log_file, make_finding
):
    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.2)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_replay_low_confidence_returns_false_none(
    store_path, git_repo, log_file, make_finding
):
    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.4)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_replay_mid_confidence_returns_false_with_pattern_id(
    store_path, git_repo, log_file, make_finding
):
    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.6)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result[0] is False
    assert result[1] == pid


def test_replay_mid_confidence_logs_hit(store_path, git_repo, log_file, make_finding):
    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.7)
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    try_replay(git_repo, finding, store, {}, log_file)

    log_content = log_file.read_text()
    assert "pattern-replay-hit" in log_content


def test_replay_high_confidence_applies_and_validates(
    store_path,
    git_repo,
    log_file,
    baseline_checks,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture") as mock_run:
        mock_run.return_value = (0, "")
        result = try_replay(git_repo, finding, store, baseline_checks, log_file)

    assert result[0] is True
    assert result[1] is not None


def test_replay_high_confidence_changes_file_content(
    store_path,
    git_repo,
    log_file,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        try_replay(git_repo, finding, store, {}, log_file)

    new_content = target.read_text()
    assert "except Exception:" in new_content
    assert "except:\n    pass" not in new_content


def test_replay_validation_passes_records_success(
    store_path,
    git_repo,
    log_file,
    baseline_checks,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        result = try_replay(git_repo, finding, store, baseline_checks, log_file)

    assert result == (True, pid)
    records = json.loads(store_path.read_text().strip().split("\n")[-1])
    assert records["success_count"] >= 1
    assert records.get("last_verified_at") is not None


def test_replay_validation_fails_reverts_and_returns_false(
    store_path,
    git_repo,
    log_file,
    baseline_checks,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    original_content = "try:\n    do_thing()\nexcept:\n    pass\n"
    target.write_text(original_content)
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    def mock_run_capture(cmd, cwd=None, timeout=0):
        if cmd == ["git", "checkout", "--", "src/example.py"]:
            subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=10)
            return (0, "")
        return (1, "lint failed")

    with patch(
        "bluei.engine.pattern_replay.run_capture",
        side_effect=mock_run_capture,
    ):
        result = try_replay(git_repo, finding, store, baseline_checks, log_file)

    assert result == (False, None)
    assert target.read_text() == original_content


def test_replay_validation_failure_records_failure(
    store_path,
    git_repo,
    log_file,
    baseline_checks,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    def mock_run_capture(cmd, cwd=None, timeout=0):
        if cmd == ["git", "checkout", "--", "src/example.py"]:
            subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=10)
            return (0, "")
        return (1, "check failed")

    with patch(
        "bluei.engine.pattern_replay.run_capture",
        side_effect=mock_run_capture,
    ):
        try_replay(git_repo, finding, store, baseline_checks, log_file)

    records = json.loads(store_path.read_text().strip().split("\n")[-1])
    assert records["skip_count"] >= 1


def test_replay_logs_success(
    store_path, git_repo, log_file, make_finding, git_commit_all
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        try_replay(git_repo, finding, store, {}, log_file)

    log_content = log_file.read_text()
    assert "pattern-replay-hit" in log_content
    assert "pattern-replay-success" in log_content


def test_replay_logs_failure(
    store_path, git_repo, log_file, baseline_checks, make_finding, git_commit_all
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    def mock_run_capture(cmd, cwd=None, timeout=0):
        if cmd == ["git", "checkout", "--", "src/example.py"]:
            subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=10)
            return (0, "")
        return (1, "fail")

    with patch(
        "bluei.engine.pattern_replay.run_capture",
        side_effect=mock_run_capture,
    ):
        try_replay(git_repo, finding, store, baseline_checks, log_file)

    log_content = log_file.read_text()
    assert "pattern-replay-hit" in log_content
    assert "pattern-replay-fail" in log_content


def test_replay_exception_returns_false_none(
    store_path, git_repo, log_file, make_finding
):
    store = MagicMock(spec=FixPatternStore)
    store.lookup.side_effect = RuntimeError("store exploded")

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_replay_snippet_not_in_file_returns_false_none(
    store_path,
    git_repo,
    log_file,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("def foo():\n    return 42\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_replay_file_not_found_returns_false_none(
    store_path,
    git_repo,
    log_file,
    make_finding,
):
    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/nonexistent.py", snippet="except:\n    pass"
    )

    result = try_replay(git_repo, finding, store, {}, log_file)

    assert result == (False, None)


def test_replay_git_checkout_used_for_revert(
    store_path,
    git_repo,
    log_file,
    baseline_checks,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    checkout_called = [False]

    def mock_run_capture(cmd, cwd=None, timeout=0):
        if cmd[0] == "git" and cmd[1] == "checkout":
            checkout_called[0] = True
            subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=10)
            return (0, "")
        return (1, "fail")

    with patch(
        "bluei.engine.pattern_replay.run_capture",
        side_effect=mock_run_capture,
    ):
        try_replay(git_repo, finding, store, baseline_checks, log_file)

    assert checkout_called[0]


def test_find_snippet_exact_match():
    text = "line1\nexcept:\n    pass\nline4\n"
    result = _find_snippet_in_file(text, "except:\n    pass")
    assert result is not None
    offset, matched_length = result
    assert text[offset : offset + matched_length] == "except:\n    pass"


def test_find_snippet_normalized_match():
    text = "line1\n    except:\n        pass\nline4\n"
    result = _find_snippet_in_file(text, "except:\n    pass")
    assert result is not None
    offset, matched_length = result
    # The matched length should reflect the original (indented) text
    assert text[offset : offset + matched_length] == "    except:\n        pass\n"


def test_find_snippet_no_match():
    text = "line1\nline2\nline3\n"
    result = _find_snippet_in_file(text, "except:\n    pass")
    assert result is None


def test_full_replay_cycle(
    store_path,
    git_repo,
    log_file,
    make_finding,
    git_commit_all,
):
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    target.write_text("try:\n    do_thing()\nexcept:\n    pass\n")
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        replayed, result_pid = try_replay(git_repo, finding, store, {}, log_file)

    assert replayed is True
    assert result_pid == pid
    assert "except Exception:" in target.read_text()


def test_replay_with_indentation_mismatch_correct_replacement(
    store_path,
    git_repo,
    log_file,
    make_finding,
    git_commit_all,
):
    """Verify replacement works when the file has extra indentation vs the stored pattern.

    This is the core bug from issue-005: the old code used len(before_snippet)
    (the normalized/less-indented version) to slice the original text, causing
    the replacement to cut too little and leave trailing characters.
    """
    src_dir = git_repo / "src"
    src_dir.mkdir()
    target = src_dir / "example.py"
    # File has 8-space indentation; pattern stores 4-space indentation
    target.write_text(
        "class Foo:\n"
        "    def bar(self):\n"
        "        try:\n"
        "            do_thing()\n"
        "        except:\n"
        "            pass\n"
    )
    git_commit_all(git_repo)

    store = FixPatternStore(store_path)
    pid = _store_pattern_with_confidence(store, 0.95)

    finding = make_finding(
        rule="broad-except",
        path="src/example.py",
        snippet="        except:\n            pass",
    )

    with patch("bluei.engine.pattern_replay.run_capture", return_value=(0, "")):
        replayed, result_pid = try_replay(git_repo, finding, store, {}, log_file)

    assert replayed is True
    new_content = target.read_text()
    # Should contain the fix (after_snippet is stored normalized by the store)
    assert "except Exception:" in new_content
    # Should NOT have leftover characters from a too-short slice
    assert "except:\n" not in new_content
    # The class and function structure should remain intact
    assert "class Foo:" in new_content
    assert "def bar(self):" in new_content
    assert "do_thing()" in new_content


# ── Merged from test_pattern_replay_remaining.py ──

import os as _os
from unittest.mock import MagicMock as _MagicMock

from bluei.engine.pattern_replay import (
    _PATTERN_CACHE,
    _clear_pattern_cache,
    _find_snippet_in_file as _find_snippet,
    _resolve_pattern,
    append_log,
    format_pattern_hint,
)


@pytest.fixture(autouse=True)
def _clear_replay_cache():
    _clear_pattern_cache()
    yield
    _clear_pattern_cache()


def test_format_pattern_hint():
    pattern = make_pattern(confidence=0.7)
    hint = format_pattern_hint(pattern)
    assert "broad-except" in hint
    assert "70%" in hint
    assert "**Before:**" in hint
    assert "**After:**" in hint
    assert "except:\n    pass" in hint
    assert "except Exception:\n    pass" in hint
    assert "Consider applying a similar transformation." in hint


def test_try_replay_exception_and_log_failure(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.side_effect = RuntimeError("boom")
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    with patch(
        "bluei.engine.pattern_replay.append_log", side_effect=OSError("log dead")
    ):
        result = try_replay(worktree, finding, store, {}, log)
    assert result == (False, None)


def test_resolve_pattern_cache_hit(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    pattern = make_pattern(confidence=0.95, pattern_id="fp-test123")
    store.lookup.return_value = pattern
    store.lookup_structural.return_value = None
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    result1 = _resolve_pattern(finding, store, log)
    assert result1 is not None
    assert result1.pattern_id == "fp-test123"
    store.lookup.reset_mock()
    result2 = _resolve_pattern(finding, store, log)
    assert result2 is not None
    store.lookup.assert_not_called()


def test_resolve_pattern_structural_hit(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.return_value = None
    pattern = make_pattern(confidence=0.95, pattern_id="fp-struct001")
    store.lookup_structural.return_value = pattern
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    result = _resolve_pattern(finding, store, log)
    assert result is not None
    assert result.pattern_id == "fp-struct001"
    log_content = log.read_text()
    assert "pattern-replay-structural-hit" in log_content
    assert "fp-struct001" in log_content


def test_resolve_pattern_fuzzy_hit(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.return_value = None
    store.lookup_structural.return_value = None
    pattern = make_pattern(confidence=0.95, pattern_id="fp-fuzzy001")
    store.lookup_fuzzy.return_value = pattern
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    with patch("bluei.engine.pattern_replay.DETECTOR_CATALOG", {}, create=True):
        with patch("bluei.engine.constants.DETECTOR_CATALOG", {}, create=True):
            result = _resolve_pattern(finding, store, log)
    assert result is not None
    assert result.pattern_id == "fp-fuzzy001"
    log_content = log.read_text()
    assert "pattern-replay-fuzzy-hit" in log_content
    assert "fp-fuzzy001" in log_content


def test_resolve_pattern_xrepo_hit(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.return_value = None
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None
    xrepo_pattern = _MagicMock()
    xrepo_pattern.pattern_id = "fp-xrepo001"
    xrepo_pattern.rule = "broad-except"
    xrepo_pattern.language = "python"
    xrepo_pattern.before_snippet = "except:\n    pass"
    xrepo_pattern.after_snippet = "except Exception:\n    pass"
    xrepo_pattern.confidence = 0.95
    xrepo_pattern.success_count = 5
    xrepo_pattern.failure_count = 0
    xrepo_pattern.source_repos = ["repo-a", "repo-b"]
    shared_library = _MagicMock()
    shared_library.lookup.return_value = xrepo_pattern
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    with patch("bluei.engine.pattern_replay.DETECTOR_CATALOG", {}, create=True):
        with patch("bluei.engine.constants.DETECTOR_CATALOG", {}, create=True):
            result = _resolve_pattern(
                finding, store, log, shared_library=shared_library
            )
    assert result is not None
    assert result.pattern_id == "fp-xrepo001"
    log_content = log.read_text()
    assert "pattern-replay-xrepo-hit" in log_content
    assert "fp-xrepo001" in log_content


def test_cache_eviction_when_over_512(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.return_value = None
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None
    for i in range(520):
        finding = make_finding(
            rule="broad-except",
            finding_id=f"finding-{i}",
            path=f"src/file_{i}.py",
            snippet=f"snippet_{i}",
        )
        _resolve_pattern(finding, store, log)
    assert len(_PATTERN_CACHE) < 520


def test_file_pattern_mismatch(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store_path = tmp_path / "state" / "fix_patterns.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = FixPatternStore(store_path)
    pattern = make_pattern(confidence=0.95, file_pattern="*.go")
    pid = store.append(pattern)
    store._rebuild_from_disk()
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    result = try_replay(worktree, finding, store, {}, log)
    assert result == (False, None)
    log_content = log.read_text()
    assert "pattern-replay-skip" in log_content
    assert "file_pattern_mismatch" in log_content


def test_find_snippet_exact_empty_snippet_returns_match():
    result = _find_snippet("some text\n", "")
    assert result is not None
    offset, length = result
    assert offset == 0
    assert length == 0


def test_append_log_propagates_on_restricted_parent(tmp_path):
    # After migration to canonical bluei.engine.state.append_log, write
    # failures are no longer swallowed. The old local helper had a
    # try/except wrapper; the canonical does not.
    log = tmp_path / "nonexistent" / "deep" / "test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    _os.chmod(str(log.parent), 0o000)
    try:
        with pytest.raises(PermissionError):
            append_log(log, "should raise")
    finally:
        _os.chmod(str(log.parent), 0o755)


def test_resolve_pattern_structural_hit_then_fuzzy_miss(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.return_value = None
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    with patch("bluei.engine.pattern_replay.DETECTOR_CATALOG", {}, create=True):
        with patch("bluei.engine.constants.DETECTOR_CATALOG", {}, create=True):
            result = _resolve_pattern(finding, store, log)
    assert result is None


def test_resolve_pattern_xrepo_no_shared_library(tmp_path, make_finding):
    log = tmp_path / "logs" / "test.log"
    store = _MagicMock(spec=FixPatternStore)
    store.lookup.return_value = None
    store.lookup_structural.return_value = None
    store.lookup_fuzzy.return_value = None
    finding = make_finding(
        rule="broad-except", path="src/example.py", snippet="except:\n    pass"
    )
    with patch("bluei.engine.pattern_replay.DETECTOR_CATALOG", {}, create=True):
        with patch("bluei.engine.constants.DETECTOR_CATALOG", {}, create=True):
            result = _resolve_pattern(finding, store, log, shared_library=None)
    assert result is None
