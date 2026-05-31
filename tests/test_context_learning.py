"""test_context_learning.py — Tests for context failure tracking and auto-skip."""

import json
from pathlib import Path

import pytest

from bluei.engine.context_fix import (
    ContextFailure,
    record_context_failure,
    load_context_failures,
    should_skip_due_to_failures,
    update_context_rule_on_repeated_failure,
    _CONTEXT_FAILURE_SKIP_THRESHOLD,
)


class TestContextFailure:
    def test_record_and_load(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        record_context_failure(fp, "ruff-e501", "app/views.py", "django", "deterministic_safe")
        record_context_failure(fp, "ruff-e501", "app/views.py", "django", "deterministic_safe")
        records = load_context_failures(fp)
        key = "ruff-e501::app/views.py::django"
        assert key in records
        assert records[key].count == 2

    def test_different_rules_tracked_separately(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        record_context_failure(fp, "ruff-e501", "a.py", "", "skip")
        record_context_failure(fp, "broad-except", "b.py", "", "skip")
        records = load_context_failures(fp)
        assert len(records) == 2

    def test_skip_threshold(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(fp, "ruff-e501", "app/views.py", "django", "deterministic_safe")
        assert should_skip_due_to_failures(fp, "ruff-e501", "app/views.py", "django") is True
        assert should_skip_due_to_failures(fp, "ruff-e501", "other.py", "django") is False

    def test_below_threshold_not_skipped(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        record_context_failure(fp, "ruff-e501", "app/views.py", "django", "deterministic_safe")
        record_context_failure(fp, "ruff-e501", "app/views.py", "django", "deterministic_safe")
        assert should_skip_due_to_failures(fp, "ruff-e501", "app/views.py", "django") is False

    def test_missing_file_returns_empty(self, tmp_path):
        fp = tmp_path / "nonexistent.jsonl"
        records = load_context_failures(fp)
        assert records == {}

    def test_corrupt_line_skipped(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        fp.write_text("not json\n", encoding="utf-8")
        records = load_context_failures(fp)
        assert records == {}


class TestAutoSkipLearning:
    def test_update_context_rule_on_repeated_failure_mutates_strategy(self, tmp_path):
        from bluei.engine.reforge import get_context_rule, reset_context_rules_cache
        reset_context_rules_cache()
        fp = tmp_path / "failures.jsonl"
        rule_name = "ruff-e501"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(fp, rule_name, "src/app.py", "django", "deterministic_safe")
        result = update_context_rule_on_repeated_failure(rule_name, "django", fp)
        assert result is True
        context_rule = get_context_rule(rule_name)
        assert context_rule is not None
        for ctx in context_rule.contexts:
            if ctx.framework == "django":
                assert ctx.fix_strategy == "skip"
                break
        reset_context_rules_cache()

    def test_below_threshold_does_not_mutate(self, tmp_path):
        from bluei.engine.reforge import get_context_rule, reset_context_rules_cache
        reset_context_rules_cache()
        fp = tmp_path / "failures.jsonl"
        record_context_failure(fp, "ruff-e501", "src/app.py", "django", "deterministic_safe")
        result = update_context_rule_on_repeated_failure("ruff-e501", "django", fp)
        assert result is False
        reset_context_rules_cache()

    def test_no_context_rule_returns_false(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(fp, "nonexistent-rule-xyz", "src/app.py", "django", "skip")
        result = update_context_rule_on_repeated_failure("nonexistent-rule-xyz", "django", fp)
        assert result is False
