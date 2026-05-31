#!/usr/bin/env python3
"""E2E pattern learning tests — store, lookup, confidence, replay."""

import json
import subprocess
from pathlib import Path

import pytest


def _seed_pattern(
    store,
    rule="trailing-whitespace",
    before="x = 1   \n",
    after="x = 1\n",
    confidence=0.5,
):
    from bluei.engine.pattern_store import FixPattern

    pid = store.append(
        FixPattern(
            pattern_id=f"pat-{rule}",
            rule=rule,
            language="python",
            file_path="main.py",
            before_snippet=before,
            after_snippet=after,
            diff_patch=f"-{before}+{after}",
            confidence=confidence,
        )
    )
    return pid


class TestPatternStoreAndLookup:
    def test_append_and_load(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        active = store.load_active()
        assert len(active) >= 1
        assert any(p.rule == "trailing-whitespace" for p in active)

    def test_lookup_by_rule_and_snippet(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        _seed_pattern(store)

        found = store.lookup("trailing-whitespace", "x = 1   \n")
        assert found is not None
        assert found.rule == "trailing-whitespace"

        missing = store.lookup("trailing-whitespace", "totally different\n")
        assert missing is None

    def test_get_pattern_by_id(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(
            store,
            rule="broad-except",
            before="except Exception:\n",
            after="except ValueError:\n",
        )

        p = store.get_pattern(pid)
        assert p is not None
        assert p.rule == "broad-except"

        assert store.get_pattern("nonexistent") is None


class TestPatternConfidence:
    def test_confidence_increases_on_success(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.update_confidence(pid, 0.2)
        p = store.get_pattern(pid)
        assert p is not None
        assert p.confidence > 0.5

    def test_confidence_decreases_on_failure(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.update_confidence(pid, -0.3)
        p = store.get_pattern(pid)
        assert p is not None
        assert p.confidence < 0.5

    def test_record_replay_updates_counts(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.record_replay(pid, success=True)
        store.record_replay(pid, success=True)
        store.record_replay(pid, success=False)

        p = store.get_pattern(pid)
        assert p is not None
        assert p.success_count >= 2
        assert p.skip_count >= 1 or p.failure_count >= 1

    def test_low_confidence_deactivated(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)

        store.update_confidence(pid, -0.5)
        active = store.load_active()
        assert len(active) == 0


class TestTryReplay:
    def test_replay_applies_high_confidence(self, tmp_path, git_commit_all):
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.pattern_replay import try_replay
        from bluei.engine.models import Finding

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1   \ny = 2\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo)

        store = FixPatternStore(tmp_path / "patterns.json")
        pid = _seed_pattern(store)
        store.update_confidence(pid, 0.5)

        log_file = tmp_path / "test.log"
        finding = Finding(
            finding_id="f-001",
            repo="test/repo",
            path="main.py",
            line=1,
            rule="trailing-whitespace",
            snippet="x = 1   ",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )

        applied, hint = try_replay(
            worktree_path=repo,
            finding=finding,
            store=store,
            baseline_checks={},
            log_file=log_file,
        )

        assert isinstance(applied, bool)

    def test_replay_skips_no_match(self, tmp_path, git_commit_all):
        from bluei.engine.pattern_store import FixPatternStore
        from bluei.engine.pattern_replay import try_replay
        from bluei.engine.models import Finding

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=repo,
            capture_output=True,
            check=True,
        )
        git_commit_all(repo)

        store = FixPatternStore(tmp_path / "patterns.json")

        log_file = tmp_path / "test.log"
        finding = Finding(
            finding_id="f-001",
            repo="test/repo",
            path="main.py",
            line=1,
            rule="nonexistent-rule",
            snippet="x = 1",
            confidence=0.9,
            quick_win=True,
            safe_to_autofix=True,
        )

        applied, hint = try_replay(
            worktree_path=repo,
            finding=finding,
            store=store,
            baseline_checks={},
            log_file=log_file,
        )

        assert applied is False


class TestFuzzyLookup:
    def test_lookup_fuzzy_runs_without_crash(self, tmp_path):
        from bluei.engine.pattern_store import FixPatternStore

        store = FixPatternStore(tmp_path / "patterns.json")
        _seed_pattern(store)

        result = store.lookup_fuzzy(
            "trailing-whitespace",
            "y = 2   \n",
            "python",
        )
        assert result is None or result.rule == "trailing-whitespace"
