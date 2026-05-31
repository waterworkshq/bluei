"""test_pattern_safety.py — Tests for framework_constraint and file_pattern on FixPattern."""

import fnmatch
from pathlib import Path

import pytest

from bluei.engine.pattern_store import FixPattern, FixPatternStore, normalize_snippet


def _make_pattern(rule="test-rule", file_path="src/app.py", framework_constraint=None, file_pattern="**/*"):
    return FixPattern(
        pattern_id="fp-test123",
        rule=rule,
        language="python",
        file_path=file_path,
        before_snippet="x = 1",
        after_snippet="x = 2",
        diff_patch="",
        framework_constraint=framework_constraint,
        file_pattern=file_pattern,
    )


class TestFrameworkConstraint:
    def test_no_constraint_matches_any_framework(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(framework_constraint=None)
        store.append(pattern)
        result = store.lookup("test-rule", normalize_snippet("x = 1"), framework="django")
        assert result is not None

    def test_constraint_matches_same_framework(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(framework_constraint="django")
        store.append(pattern)
        result = store.lookup("test-rule", normalize_snippet("x = 1"), framework="django")
        assert result is not None

    def test_constraint_rejects_different_framework(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(framework_constraint="django")
        store.append(pattern)
        result = store.lookup("test-rule", normalize_snippet("x = 1"), framework="flask")
        assert result is None

    def test_constraint_no_framework_param_still_matches(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(framework_constraint="django")
        store.append(pattern)
        result = store.lookup("test-rule", normalize_snippet("x = 1"), framework=None)
        assert result is not None


class TestFilePattern:
    def test_star_star_pattern_matches_anything(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(file_pattern="**/*")
        store.append(pattern)
        assert fnmatch.fnmatch("src/app.py", pattern.file_pattern)
        assert fnmatch.fnmatch("tests/test_foo.py", pattern.file_pattern)

    def test_src_pattern_excludes_tests(self):
        assert fnmatch.fnmatch("src/app.py", "src/*")
        assert not fnmatch.fnmatch("tests/test_foo.py", "src/*")

    def test_default_pattern_backward_compat(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        old_record = '{"pattern_id":"fp-old","rule":"test-rule","language":"python","file_path":"src/app.py","before_snippet":"x = 1","after_snippet":"x = 2","diff_patch":"","confidence":0.9,"success_count":1,"failure_count":0,"skip_count":0,"source":"autofix"}'
        store_path.write_text(old_record + "\n", encoding="utf-8")
        store = FixPatternStore(store_path)
        result = store.lookup("test-rule", normalize_snippet("x = 1"))
        assert result is not None
        assert result.file_pattern == "**/*"
        assert result.framework_constraint is None
