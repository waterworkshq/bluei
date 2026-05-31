import subprocess
from pathlib import Path

import pytest

from bluei.engine.fix_tiers import FixTier
from bluei.engine.lifecycle import _tier_validate, apply_autofix, apply_cascade_fix
from bluei.engine.models import Finding
from bluei.engine.pattern_replay import (
    AUTO_REPLAY_THRESHOLD,
    _clear_pattern_cache,
    try_replay,
)
from bluei.engine.pattern_store import FixPattern, FixPatternStore, INITIAL_CONFIDENCE
from bluei.engine.reforge import RefactorClass, classify_finding


def _create_fixable_repo(tmp_path: Path, git_repo, git_commit_all) -> Path:
    repo = git_repo
    src = repo / "src"
    src.mkdir(exist_ok=True)
    (src / "__init__.py").write_text("")
    (src / "trailing.py").write_text("def hello():\n    x = 1   \n    return x  \n\n")
    (src / "popfront.py").write_text(
        "def process(items):\n"
        "    for idx, item in enumerate(items):\n"
        "        entry = items.pop(0)\n"
        "        if entry is None:\n"
        "            break\n"
        "    return items\n"
    )
    git_commit_all(repo)
    return repo


def test_recipe_trailing_whitespace_fix(
    tmp_path, make_finding, git_repo, git_commit_all
):
    repo = _create_fixable_repo(tmp_path, git_repo, git_commit_all)
    log_file = tmp_path / "test.log"
    target = repo / "src" / "trailing.py"

    original = target.read_text()
    assert "   \n" in original or "  \n" in original

    finding = make_finding(
        rule="trailing-whitespace",
        path="src/trailing.py",
        line=2,
        snippet="    x = 1   ",
    )

    result = apply_autofix(repo, finding, log_file)

    assert result is True
    fixed = target.read_text()
    for line in fixed.splitlines():
        assert line == line.rstrip(), f"trailing whitespace remains: {line!r}"


def test_cascade_exhaustion_rollback(tmp_path, make_finding, git_repo, git_commit_all):
    repo = _create_fixable_repo(tmp_path, git_repo, git_commit_all)
    log_file = tmp_path / "test.log"
    target = repo / "src" / "trailing.py"
    original = target.read_text()

    finding = make_finding(
        rule="completely-unknown-rule-xyz",
        path="src/trailing.py",
        line=1,
        snippet="def hello():",
    )

    result = apply_cascade_fix(
        repo,
        finding,
        log_file,
        deterministic_only=True,
    )

    assert result is False
    assert target.read_text() == original


def test_verification_failure_rollback(
    tmp_path, make_finding, git_repo, git_commit_all
):
    repo = git_repo
    (repo / "src").mkdir(exist_ok=True)

    valid_content = "def good():\n    return 42\n"
    (repo / "src" / "target.py").write_text(valid_content)
    git_commit_all(repo)

    broken_content = "def good():\n    return 42\n   broken syntax {\n"
    (repo / "src" / "target.py").write_text(broken_content)

    log_file = tmp_path / "test.log"
    finding = make_finding(
        rule="trailing-whitespace",
        path="src/target.py",
        line=1,
        snippet="def good():",
    )

    passed = _tier_validate(FixTier.T0_GUARANTEED, repo, finding, log_file)

    assert passed is False
    assert (repo / "src" / "target.py").read_text() == valid_content


def test_pattern_store_and_lookup(tmp_path):
    store_path = tmp_path / "patterns.jsonl"
    store = FixPatternStore(store_path)

    pattern = FixPattern(
        pattern_id="fp-store-test",
        rule="trailing-whitespace",
        language="python",
        file_path="src/trailing.py",
        before_snippet="    x = 1   ",
        after_snippet="    x = 1",
        diff_patch="--- a\n+++ b\n",
    )

    pid = store.append(pattern)
    assert pid.startswith("fp-")

    hit = store.lookup("trailing-whitespace", "    x = 1   ")
    assert hit is not None
    assert hit.rule == "trailing-whitespace"
    assert hit.pattern_id == pid

    active = store.load_active(rule="trailing-whitespace")
    assert len(active) >= 1

    miss = store.lookup("nonexistent-rule", "    x = 1   ")
    assert miss is None


def test_pattern_confidence_lifecycle(tmp_path, make_finding, git_repo, git_commit_all):
    repo = git_repo
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "fixme.py").write_text("x = old_value\n")
    git_commit_all(repo)

    store_path = tmp_path / "patterns.jsonl"
    store = FixPatternStore(store_path)

    pattern = FixPattern(
        pattern_id="fp-conf",
        rule="test-rule",
        language="python",
        file_path="src/fixme.py",
        before_snippet="x = old_value",
        after_snippet="x = new_value",
        diff_patch="",
        file_pattern="src/*",
    )

    pid = store.append(pattern)
    _clear_pattern_cache()

    finding = make_finding(
        rule="test-rule",
        path="src/fixme.py",
        line=1,
        snippet="x = old_value",
    )
    log_file = tmp_path / "test.log"

    success, hint_pid = try_replay(repo, finding, store, {}, log_file)
    assert success is False
    assert hint_pid == pid

    store.update_confidence(pid, 0.5)
    _clear_pattern_cache()

    success, replay_pid = try_replay(repo, finding, store, {}, log_file)
    assert success is True
    assert replay_pid == pid

    assert "new_value" in (repo / "src" / "fixme.py").read_text()
    assert "old_value" not in (repo / "src" / "fixme.py").read_text()

    fetched = store.get_pattern(pid)
    assert fetched is not None
    assert fetched.confidence >= AUTO_REPLAY_THRESHOLD
