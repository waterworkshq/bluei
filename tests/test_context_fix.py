import builtins
import json
from pathlib import Path

import pytest
from unittest.mock import patch

from bluei.engine.context_fix import (
    _load_lifecycle_functions,
    load_context_failures,
    record_context_failure,
    update_context_rule_on_repeated_failure,
    _CONTEXT_FAILURE_SKIP_THRESHOLD,
)
from bluei.engine.reforge import get_context_rule, reset_context_rules_cache


@pytest.fixture(autouse=True)
def _reset_context_cache():
    reset_context_rules_cache()
    yield
    reset_context_rules_cache()


class TestLoadLifecycleFunctions:
    def test_import_error_raises_module_not_found(self):
        _real_import = builtins.__import__

        def _block_lifecycle(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "bluei.engine" and fromlist and "lifecycle" in fromlist:
                raise ImportError("blocked for test")
            return _real_import(name, globals, locals, fromlist, level)

        with patch.object(builtins, "__import__", _block_lifecycle):
            with pytest.raises(ModuleNotFoundError, match="lifecycle module not found"):
                _load_lifecycle_functions()

    def test_returns_real_functions_when_no_mocks(self):
        from bluei.engine.lifecycle import (
            ClaudeFixRequest,
            apply_autofix,
            apply_claude_fix,
        )

        result_autofix, result_claude_fix, result_claude_req = (
            _load_lifecycle_functions()
        )
        assert result_autofix is apply_autofix
        assert result_claude_fix is apply_claude_fix
        assert result_claude_req is ClaudeFixRequest


class TestLoadContextFailuresEmptyLine:
    def test_empty_lines_and_whitespace_only_lines_skipped(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        valid = json.dumps(
            {
                "rule": "ruff-e501",
                "file_path": "a.py",
                "framework": "django",
                "strategy": "deterministic_safe",
                "count": 1,
            }
        )
        fp.write_text(f"\n  \n{valid}\n\n", encoding="utf-8")
        records = load_context_failures(fp)
        assert len(records) == 1
        assert "ruff-e501::a.py::django" in records

    def test_trailing_empty_lines_ignored(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        valid = json.dumps(
            {
                "rule": "broad-except",
                "file_path": "b.py",
                "framework": "flask",
                "strategy": "skip",
                "count": 2,
            }
        )
        fp.write_text(f"{valid}\n\n\n", encoding="utf-8")
        records = load_context_failures(fp)
        assert len(records) == 1
        assert records["broad-except::b.py::flask"].count == 2


class TestUpdateContextRuleOnRepeatedFailure:
    def test_matching_framework_context_set_to_skip(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        rule_name = "ruff-b904"
        framework = "django"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(
                fp, rule_name, "src/middleware.py", framework, "llm_with_context"
            )

        context_rule = get_context_rule(rule_name)
        assert context_rule is not None
        django_ctx = [c for c in context_rule.contexts if c.framework == framework]
        assert len(django_ctx) == 1
        original_strategy = django_ctx[0].fix_strategy
        assert original_strategy != "skip"

        result = update_context_rule_on_repeated_failure(rule_name, framework, fp)
        assert result is True
        assert django_ctx[0].fix_strategy == "skip"

    def test_matching_any_framework_context_set_to_skip(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        rule_name = "ruff-c408"
        framework = "nonexistent-framework-xyz"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(
                fp, rule_name, "src/app.py", framework, "deterministic"
            )

        context_rule = get_context_rule(rule_name)
        assert context_rule is not None
        any_ctx = [c for c in context_rule.contexts if c.framework == "any"]
        assert len(any_ctx) == 1
        assert any_ctx[0].fix_strategy != "skip"

        result = update_context_rule_on_repeated_failure(rule_name, framework, fp)
        assert result is True
        assert any_ctx[0].fix_strategy == "skip"

    def test_default_strategy_set_to_skip_when_all_contexts_already_skip(
        self, tmp_path
    ):
        fp = tmp_path / "failures.jsonl"
        rule_name = "ruff-e501"
        framework = "django"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(
                fp, rule_name, "src/app.py", framework, "deterministic_safe"
            )

        context_rule = get_context_rule(rule_name)
        assert context_rule is not None
        for ctx in context_rule.contexts:
            assert ctx.fix_strategy == "skip"
        assert context_rule.default_strategy != "skip"

        result = update_context_rule_on_repeated_failure(rule_name, framework, fp)
        assert result is True
        assert context_rule.default_strategy == "skip"

    def test_returns_false_when_everything_already_skip(self, tmp_path):
        fp = tmp_path / "failures.jsonl"
        rule_name = "ruff-c408"
        framework = "django"
        for _ in range(_CONTEXT_FAILURE_SKIP_THRESHOLD):
            record_context_failure(
                fp, rule_name, "src/app.py", framework, "deterministic"
            )

        context_rule = get_context_rule(rule_name)
        assert context_rule is not None
        for ctx in context_rule.contexts:
            ctx.fix_strategy = "skip"
        context_rule.default_strategy = "skip"

        result = update_context_rule_on_repeated_failure(rule_name, framework, fp)
        assert result is False
