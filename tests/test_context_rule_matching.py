#!/usr/bin/env python3
"""Tests for context rule matching: get_context_rule, match_context, classify_finding routing."""

import pytest

from bluei.engine.constants import CONTEXT_RULES
from bluei.engine.models import Finding
from bluei.engine.reforge import (
    ContextOverride,
    ContextRule,
    RefactorClass,
    classify_finding,
    get_context_rule,
    match_context,
    reset_context_rules_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_context_rules_cache()
    yield
    reset_context_rules_cache()


def _finding(
    path: str = "src/app.py",
    rule: str = "ruff-c408",
    safe_to_autofix: bool = False,
    finding_id: str = "f-001",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        repo="test-repo",
        path=path,
        line=10,
        rule=rule,
        snippet="x = 1",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=safe_to_autofix,
    )


RULE_NAMES_WITH_CONTEXT = [entry["rule"] for entry in CONTEXT_RULES]


class TestGetContextRule:
    def test_returns_rule_for_known_rule(self):
        result = get_context_rule("ruff-c408")
        assert result is not None
        assert result.rule == "ruff-c408"

    def test_returns_none_for_unknown_rule(self):
        assert get_context_rule("nonexistent-rule-xyz") is None

    def test_returns_none_for_empty_string(self):
        assert get_context_rule("") is None

    def test_returns_rule_for_every_defined_context_rule(self):
        for rule_name in RULE_NAMES_WITH_CONTEXT:
            result = get_context_rule(rule_name)
            assert result is not None, f"Expected rule for {rule_name}"
            assert result.rule == rule_name

    def test_default_strategy_populated(self):
        for entry in CONTEXT_RULES:
            rule = get_context_rule(entry["rule"])
            assert rule is not None
            assert rule.default_strategy == entry["default_strategy"]

    def test_contexts_populated_as_context_override_objects(self):
        rule = get_context_rule("ruff-c408")
        assert rule is not None
        assert len(rule.contexts) >= 1
        for ctx in rule.contexts:
            assert isinstance(ctx, ContextOverride)
            assert isinstance(ctx.file_patterns, list)
            assert len(ctx.file_patterns) >= 1
            assert isinstance(ctx.framework, str)
            assert isinstance(ctx.fix_strategy, str)

    def test_caches_result(self):
        first = get_context_rule("ruff-c408")
        second = get_context_rule("ruff-c408")
        assert first is second

    def test_b904_rule_loaded(self):
        rule = get_context_rule("ruff-b904")
        assert rule is not None
        assert rule.default_strategy == "llm_with_context"

    def test_s311_rule_loaded(self):
        rule = get_context_rule("ruff-s311")
        assert rule is not None
        assert rule.default_strategy == "skip"

    def test_e501_rule_loaded(self):
        rule = get_context_rule("ruff-e501")
        assert rule is not None
        assert rule.default_strategy == "deterministic_safe"

    def test_broad_except_rule_loaded(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        assert rule.default_strategy == "llm_with_context"

    def test_type_explicit_any_rule_loaded(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        assert rule.default_strategy == "llm_with_context"

    def test_xo_max_lines_rule_loaded(self):
        rule = get_context_rule("xo-max-lines")
        assert rule is not None
        assert rule.default_strategy == "skip"

    def test_xo_complexity_rule_loaded(self):
        rule = get_context_rule("xo-complexity")
        assert rule is not None
        assert rule.default_strategy == "skip"

    def test_trailing_whitespace_rule_loaded(self):
        rule = get_context_rule("trailing-whitespace")
        assert rule is not None
        assert rule.default_strategy == "deterministic_safe"


class TestMatchContextFilePatterns:
    def test_migration_glob_match(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001_initial.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_migration_glob_no_match_different_framework(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001_initial.py", rule, detected_frameworks=["flask"]
        )
        assert ctx is None

    def test_migration_glob_no_match_no_frameworks(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001_initial.py", rule, detected_frameworks=None
        )
        assert ctx is None

    def test_test_prefix_glob_match(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "deterministic_safe"

    def test_test_suffix_glob_match(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/views_test.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "deterministic_safe"

    def test_tests_directory_glob_match(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/tests/test_views.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "deterministic_safe"

    def test_no_match_for_app_code(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/views.py", rule)
        assert ctx is None

    def test_no_match_for_empty_path(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("", rule)
        assert ctx is None

    def test_multiple_patterns_first_match_wins(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_taskqueue_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context("src/taskqueue_worker.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_celery_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context("src/celery_tasks.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_tasks_dir_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context("src/tasks/email_task.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_workers_dir_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context("src/workers/bg_worker.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_test_file_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context("tests/test_main.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_test_suffix_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context("src/app_test.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_middleware_django_pattern(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context(
            "src/middleware_auth.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_broad_except_middleware_non_django(self):
        rule = get_context_rule("broad-except")
        assert rule is not None
        ctx = match_context(
            "src/middleware_auth.py", rule, detected_frameworks=["flask"]
        )
        assert ctx is None

    def test_trailing_whitespace_md_skip(self):
        rule = get_context_rule("trailing-whitespace")
        assert rule is not None
        ctx = match_context("docs/README.md", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_trailing_whitespace_nested_md_skip(self):
        rule = get_context_rule("trailing-whitespace")
        assert rule is not None
        ctx = match_context("docs/api/v2/endpoints.md", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_trailing_whitespace_rst_skip(self):
        rule = get_context_rule("trailing-whitespace")
        assert rule is not None
        ctx = match_context("docs/index.rst", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_trailing_whitespace_txt_skip(self):
        rule = get_context_rule("trailing-whitespace")
        assert rule is not None
        ctx = match_context("notes.txt", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_trailing_whitespace_py_no_skip(self):
        rule = get_context_rule("trailing-whitespace")
        assert rule is not None
        ctx = match_context("src/app.py", rule)
        assert ctx is None

    def test_hardcoded_tmp_test_file_skip(self):
        rule = get_context_rule("hardcoded-tmp-path")
        assert rule is not None
        ctx = match_context("tests/test_utils.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_hardcoded_tmp_conftest_skip(self):
        rule = get_context_rule("hardcoded-tmp-path")
        assert rule is not None
        ctx = match_context("tests/conftest.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_hardcoded_tmp_script_llm(self):
        rule = get_context_rule("hardcoded-tmp-path")
        assert rule is not None
        ctx = match_context("scripts/report.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "llm_with_context"

    def test_hardcoded_tmp_tools_llm(self):
        rule = get_context_rule("hardcoded-tmp-path")
        assert rule is not None
        ctx = match_context("tools/build.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "llm_with_context"

    def test_hardcoded_tmp_app_code_no_match(self):
        rule = get_context_rule("hardcoded-tmp-path")
        assert rule is not None
        ctx = match_context("src/app.py", rule)
        assert ctx is None

    def test_xo_max_lines_generated_skip(self):
        rule = get_context_rule("xo-max-lines")
        assert rule is not None
        for path in [
            "src/generated/api.ts",
            "generated/client.ts",
            "src/auto-generated/models.ts",
            "auto-generated/schema.ts",
        ]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "skip"

    def test_xo_max_lines_migration_skip(self):
        rule = get_context_rule("xo-max-lines")
        assert rule is not None
        ctx = match_context("migrations/001_create_users.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_xo_max_lines_seeds_skip(self):
        rule = get_context_rule("xo-max-lines")
        assert rule is not None
        ctx = match_context("seeds/initial_data.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_xo_max_lines_generated_ts_skip(self):
        rule = get_context_rule("xo-max-lines")
        assert rule is not None
        ctx = match_context("src/api.generated.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_type_explicit_any_dts_skip(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        ctx = match_context("src/types/global.d.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_type_explicit_any_types_dir_skip(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        ctx = match_context("src/types/index.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_type_explicit_any_typings_dir_skip(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        ctx = match_context("src/typings/env.d.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_type_explicit_any_test_file_deterministic_safe(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        for path in [
            "src/test/utils.test.ts",
            "src/__tests__/helpers.spec.ts",
            "src/test/main.ts",
        ]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "deterministic_safe"

    def test_type_explicit_any_migration_skip(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        ctx = match_context("migrations/001_init.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_type_explicit_any_config_skip(self):
        rule = get_context_rule("type-explicit-any")
        assert rule is not None
        for path in [
            "next.config.js",
            "webpack.config.ts",
            "vite.config.ts",
            "rollup.config.mjs",
        ]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "skip"

    def test_xo_complexity_parser_llm(self):
        rule = get_context_rule("xo-complexity")
        assert rule is not None
        ctx = match_context("src/parsers/json_parser.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "llm_with_context"

    def test_xo_complexity_serializer_llm(self):
        rule = get_context_rule("xo-complexity")
        assert rule is not None
        ctx = match_context("src/serializers/xml_serializer.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "llm_with_context"

    def test_xo_complexity_deserializer_llm(self):
        rule = get_context_rule("xo-complexity")
        assert rule is not None
        ctx = match_context("src/deserializers/csv_reader.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "llm_with_context"

    def test_xo_complexity_generated_skip(self):
        rule = get_context_rule("xo-complexity")
        assert rule is not None
        ctx = match_context("src/generated/ast.ts", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_xo_no_warning_comments_test_skip(self):
        rule = get_context_rule("xo-no-warning-comments")
        assert rule is not None
        for path in ["src/test/main.ts", "src/__tests__/app.test.ts"]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "skip"

    def test_test_gap_migrations_skip(self):
        rule = get_context_rule("test-gap-missing-file")
        assert rule is not None
        for path in ["migrations/001.py", "alembic/versions/abc123.py"]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "skip"

    def test_test_gap_pycache_skip(self):
        rule = get_context_rule("test-gap-missing-file")
        assert rule is not None
        ctx = match_context("src/__pycache__/mod.cpython-311.pyc", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_test_gap_node_modules_skip(self):
        rule = get_context_rule("test-gap-missing-file")
        assert rule is not None
        ctx = match_context("node_modules/lodash/index.js", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_test_gap_venv_skip(self):
        rule = get_context_rule("test-gap-missing-file")
        assert rule is not None
        ctx = match_context(".venv/lib/python3.11/site.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_test_coverage_line_migrations_skip(self):
        rule = get_context_rule("test-coverage-line")
        assert rule is not None
        for path in [
            "migrations/001.py",
            "alembic/versions/abc.py",
            "generated/proto_pb2.py",
        ]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "skip"

    def test_test_coverage_line_conftest_skip(self):
        rule = get_context_rule("test-coverage-line")
        assert rule is not None
        ctx = match_context("tests/conftest.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_test_coverage_line_fixtures_skip(self):
        rule = get_context_rule("test-coverage-line")
        assert rule is not None
        ctx = match_context("tests/fixtures/data.py", rule)
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_perf_pop_front_migration_skip(self):
        rule = get_context_rule("perf-pop-front-loop")
        assert rule is not None
        ctx = match_context(
            "app/migrations/0002_data.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert ctx.fix_strategy == "skip"

    def test_perf_pop_front_migration_no_framework(self):
        rule = get_context_rule("perf-pop-front-loop")
        assert rule is not None
        ctx = match_context("app/migrations/0002_data.py", rule)
        assert ctx is None

    def test_perf_list_membership_test_skip(self):
        rule = get_context_rule("perf-list-membership-loop")
        assert rule is not None
        for path in ["tests/test_perf.py", "src/app_test.py"]:
            ctx = match_context(path, rule)
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "skip"

    def test_type_missing_return_nextjs_api_llm(self):
        rule = get_context_rule("type-missing-return")
        assert rule is not None
        for path in ["src/pages/api/users.ts", "src/app/api/auth/route.ts"]:
            ctx = match_context(path, rule, detected_frameworks=["nextjs"])
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "llm_with_context"

    def test_type_missing_return_nextjs_no_framework(self):
        rule = get_context_rule("type-missing-return")
        assert rule is not None
        ctx = match_context("src/pages/api/users.ts", rule)
        assert ctx is None

    def test_type_missing_return_express_llm(self):
        rule = get_context_rule("type-missing-return")
        assert rule is not None
        for path in [
            "src/controllers/user_controller.ts",
            "src/handlers/auth_handler.ts",
        ]:
            ctx = match_context(path, rule, detected_frameworks=["express"])
            assert ctx is not None, f"Expected match for {path}"
            assert ctx.fix_strategy == "llm_with_context"

    def test_type_missing_return_express_no_framework(self):
        rule = get_context_rule("type-missing-return")
        assert rule is not None
        ctx = match_context("src/controllers/user_controller.ts", rule)
        assert ctx is None


class TestMatchContextFrameworkDetection:
    def test_any_framework_matches_without_detected_frameworks(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule, detected_frameworks=None)
        assert ctx is not None
        assert ctx.framework == "any"

    def test_any_framework_matches_with_any_detected_frameworks(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule, detected_frameworks=["flask"])
        assert ctx is not None

    def test_any_framework_matches_with_empty_list(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule, detected_frameworks=[])
        assert ctx is not None

    def test_django_framework_requires_django(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert ctx.framework == "django"

    def test_django_framework_rejects_flask(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001.py", rule, detected_frameworks=["flask"]
        )
        assert ctx is None

    def test_django_framework_rejects_none(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("app/migrations/0001.py", rule, detected_frameworks=None)
        assert ctx is None

    def test_django_framework_rejects_empty(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("app/migrations/0001.py", rule, detected_frameworks=[])
        assert ctx is None

    def test_nextjs_framework_requires_nextjs(self):
        rule = get_context_rule("type-missing-return")
        ctx = match_context(
            "src/pages/api/users.ts", rule, detected_frameworks=["nextjs"]
        )
        assert ctx is not None
        assert ctx.framework == "nextjs"

    def test_nextjs_framework_rejects_express(self):
        rule = get_context_rule("type-missing-return")
        ctx = match_context(
            "src/pages/api/users.ts", rule, detected_frameworks=["express"]
        )
        assert ctx is None

    def test_express_framework_requires_express(self):
        rule = get_context_rule("type-missing-return")
        ctx = match_context(
            "src/controllers/user.ts", rule, detected_frameworks=["express"]
        )
        assert ctx is not None
        assert ctx.framework == "express"

    def test_express_framework_rejects_nextjs(self):
        rule = get_context_rule("type-missing-return")
        ctx = match_context(
            "src/controllers/user.ts", rule, detected_frameworks=["nextjs"]
        )
        assert ctx is None

    def test_multiple_frameworks_django_in_list(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001.py",
            rule,
            detected_frameworks=["flask", "django", "fastapi"],
        )
        assert ctx is not None
        assert ctx.framework == "django"

    def test_b904_middleware_django_specific(self):
        rule = get_context_rule("ruff-b904")
        assert rule is not None
        ctx = match_context(
            "src/middleware_auth.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert ctx.framework == "django"

    def test_b904_middleware_non_django_falls_to_default(self):
        rule = get_context_rule("ruff-b904")
        assert rule is not None
        ctx = match_context(
            "src/middleware_auth.py", rule, detected_frameworks=["flask"]
        )
        assert ctx is None


class TestMatchContextReturnsCorrectObject:
    def test_returns_context_override_type(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule)
        assert isinstance(ctx, ContextOverride)

    def test_prompt_hint_populated(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule)
        assert ctx is not None
        assert ctx.prompt_hint is not None
        assert isinstance(ctx.prompt_hint, str)
        assert len(ctx.prompt_hint) > 0

    def test_migration_prompt_hint_content(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context(
            "app/migrations/0001.py", rule, detected_frameworks=["django"]
        )
        assert ctx is not None
        assert (
            "migration" in ctx.prompt_hint.lower()
            or "dict()" in ctx.prompt_hint.lower()
        )

    def test_file_patterns_accessible(self):
        rule = get_context_rule("ruff-c408")
        ctx = match_context("zerver/test_views.py", rule)
        assert ctx is not None
        assert (
            "**/test_*.py" in ctx.file_patterns or "**/*_test.py" in ctx.file_patterns
        )


class TestClassifyFindingContextRouting:
    def test_c408_django_migration_skip_routes_to_refactor_class(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.REFACTOR_CLASS
        assert f.refactor_class == "refactor_class"

    def test_c408_test_file_routes_to_simple_fix(self):
        f = _finding(path="zerver/test_views.py", rule="ruff-c408")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX
        assert f.refactor_class == "simple_fix"

    def test_c408_app_code_routes_to_default(self):
        f = _finding(path="zerver/views.py", rule="ruff-c408")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_b904_default_routes_to_contextual(self):
        f = _finding(path="src/views.py", rule="ruff-b904")
        result = classify_finding(f)
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_b904_middleware_routes_to_contextual(self):
        f = _finding(path="src/middleware.py", rule="ruff-b904")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_b007_fixtures_routes_to_simple(self):
        f = _finding(path="src/fixtures.py", rule="ruff-b007")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_b007_default_routes_to_contextual(self):
        f = _finding(path="src/views.py", rule="ruff-b007")
        result = classify_finding(f)
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_s311_test_file_routes_to_simple(self):
        f = _finding(path="tests/test_app.py", rule="ruff-s311")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_s311_default_skip_routes_to_claude(self):
        f = _finding(path="src/crypto.py", rule="ruff-s311")
        result = classify_finding(f)
        assert result == RefactorClass.CLAUDE_FIX

    def test_e501_migration_django_routes_to_refactor(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-e501")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.REFACTOR_CLASS

    def test_e501_urls_routes_to_refactor(self):
        f = _finding(path="app/urls.py", rule="ruff-e501")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_e501_routing_routes_to_refactor(self):
        f = _finding(path="app/routing.py", rule="ruff-e501")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_e501_default_routes_to_simple(self):
        f = _finding(path="src/views.py", rule="ruff-e501")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_broad_except_taskqueue_routes_to_refactor(self):
        f = _finding(path="src/taskqueue_worker.py", rule="broad-except")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_broad_except_test_routes_to_refactor(self):
        f = _finding(path="tests/test_main.py", rule="broad-except")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_broad_except_default_routes_to_contextual(self):
        f = _finding(path="src/views.py", rule="broad-except")
        result = classify_finding(f)
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_broad_except_middleware_django_routes_to_refactor(self):
        f = _finding(path="src/middleware_auth.py", rule="broad-except")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.REFACTOR_CLASS

    def test_broad_except_middleware_flask_default(self):
        f = _finding(path="src/middleware_auth.py", rule="broad-except")
        result = classify_finding(f, detected_frameworks=["flask"])
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_trailing_whitespace_md_routes_to_refactor(self):
        f = _finding(path="docs/README.md", rule="trailing-whitespace")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_trailing_whitespace_py_default_routes_to_simple(self):
        f = _finding(path="src/app.py", rule="trailing-whitespace")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_hardcoded_tmp_test_routes_to_refactor(self):
        f = _finding(path="tests/test_utils.py", rule="hardcoded-tmp-path")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_hardcoded_tmp_script_routes_to_contextual(self):
        f = _finding(path="scripts/report.py", rule="hardcoded-tmp-path")
        result = classify_finding(f)
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_hardcoded_tmp_default_routes_to_simple(self):
        f = _finding(path="src/app.py", rule="hardcoded-tmp-path")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_type_explicit_any_dts_routes_to_refactor(self):
        f = _finding(path="src/types/global.d.ts", rule="type-explicit-any")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_type_explicit_any_test_routes_to_simple(self):
        f = _finding(path="src/test/utils.test.ts", rule="type-explicit-any")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_type_explicit_any_default_routes_to_contextual(self):
        f = _finding(path="src/app.ts", rule="type-explicit-any")
        result = classify_finding(f)
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_type_missing_return_nextjs_routes_to_contextual(self):
        f = _finding(path="src/pages/api/users.ts", rule="type-missing-return")
        result = classify_finding(f, detected_frameworks=["nextjs"])
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_type_missing_return_express_routes_to_contextual(self):
        f = _finding(path="src/controllers/user.ts", rule="type-missing-return")
        result = classify_finding(f, detected_frameworks=["express"])
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_type_missing_return_default_routes_to_simple(self):
        f = _finding(path="src/utils.ts", rule="type-missing-return")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX


class TestClassifyFindingOverrideBehavior:
    def test_xo_max_lines_always_refactor_class(self):
        f = _finding(path="src/hooks.ts", rule="xo-max-lines")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_xo_max_lines_generated_still_refactor(self):
        f = _finding(path="generated/api.ts", rule="xo-max-lines")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_xo_complexity_always_refactor_class(self):
        f = _finding(path="src/hooks.ts", rule="xo-complexity")
        result = classify_finding(f)
        assert result == RefactorClass.REFACTOR_CLASS

    def test_claude_fix_rules_after_context(self):
        f = _finding(path="src/app.py", rule="test-coverage-branch")
        result = classify_finding(f)
        assert result == RefactorClass.CLAUDE_FIX

    def test_claude_fix_function_coverage(self):
        f = _finding(path="src/app.py", rule="test-coverage-function")
        result = classify_finding(f)
        assert result == RefactorClass.CLAUDE_FIX

    def test_unknown_rule_cascades(self):
        f = _finding(path="src/app.py", rule="totally-unknown-rule")
        result = classify_finding(f)
        assert result == RefactorClass.CASCADE_FIX

    def test_ruff_rule_with_safe_autofix_no_context(self):
        f = _finding(path="src/app.py", rule="ruff-xyz", safe_to_autofix=True)
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_ruff_rule_without_safe_autofix_no_context(self):
        f = _finding(path="src/app.py", rule="ruff-xyz", safe_to_autofix=False)
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_context_match_overrides_default(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-e501")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.REFACTOR_CLASS

    def test_no_context_match_uses_default(self):
        f = _finding(path="src/app.py", rule="ruff-e501")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_skip_strategy_routes_to_refactor_class(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.REFACTOR_CLASS

    def test_llm_with_context_strategy_routes_to_contextual(self):
        f = _finding(path="src/middleware.py", rule="ruff-b904")
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.CONTEXTUAL_FIX

    def test_deterministic_safe_routes_to_simple(self):
        f = _finding(path="src/fixtures.py", rule="ruff-b007")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX


class TestClassifyFindingSetsRefactorClassField:
    def test_sets_refactor_class_on_finding(self):
        f = _finding(path="zerver/views.py", rule="ruff-c408")
        assert f.refactor_class is None
        classify_finding(f)
        assert f.refactor_class is not None

    def test_refactor_class_value_matches_return(self):
        f = _finding(path="zerver/views.py", rule="ruff-c408")
        result = classify_finding(f)
        assert f.refactor_class == result.value

    def test_sets_simple_fix_value(self):
        f = _finding(path="zerver/test_views.py", rule="ruff-c408")
        classify_finding(f)
        assert f.refactor_class == "simple_fix"

    def test_sets_refactor_class_value(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        classify_finding(f, detected_frameworks=["django"])
        assert f.refactor_class == "refactor_class"

    def test_sets_contextual_fix_value(self):
        f = _finding(path="src/views.py", rule="ruff-b904")
        classify_finding(f)
        assert f.refactor_class == "contextual_fix"

    def test_sets_claude_fix_value(self):
        f = _finding(path="src/app.py", rule="test-coverage-branch")
        classify_finding(f)
        assert f.refactor_class == "claude_fix"

    def test_sets_cascade_fix_value(self):
        f = _finding(path="src/app.py", rule="unknown-rule")
        classify_finding(f)
        assert f.refactor_class == "cascade_fix"


class TestClassifyFindingEdgeCases:
    def test_finding_with_empty_path(self):
        f = _finding(path="", rule="ruff-c408")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_finding_with_none_path_attr(self):
        f = _finding(path="src/app.py", rule="ruff-c408")

        class NoPath:
            rule = "ruff-c408"
            refactor_class = None

        nf = NoPath()
        result = classify_finding(nf)
        assert isinstance(result, RefactorClass)

    def test_finding_with_empty_rule(self):
        f = _finding(rule="")
        result = classify_finding(f)
        assert result == RefactorClass.CASCADE_FIX

    def test_finding_with_none_rule(self):
        class NoneRule:
            rule = None
            path = "src/app.py"
            safe_to_autofix = False
            refactor_class = None

        nf = NoneRule()
        result = classify_finding(nf)
        assert result == RefactorClass.CASCADE_FIX

    def test_framework_list_with_multiple_entries(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        result = classify_finding(f, detected_frameworks=["flask", "django", "pytest"])
        assert result == RefactorClass.REFACTOR_CLASS

    def test_framework_list_without_match(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        result = classify_finding(f, detected_frameworks=["flask", "pytest"])
        assert result == RefactorClass.SIMPLE_FIX

    def test_empty_framework_list(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        result = classify_finding(f, detected_frameworks=[])
        assert result == RefactorClass.SIMPLE_FIX

    def test_none_framework_list(self):
        f = _finding(path="app/migrations/0001.py", rule="ruff-c408")
        result = classify_finding(f, detected_frameworks=None)
        assert result == RefactorClass.SIMPLE_FIX

    def test_deeply_nested_migration_path(self):
        f = _finding(
            path="project/apps/core/migrations/0042_auto_20240101.py", rule="ruff-c408"
        )
        result = classify_finding(f, detected_frameworks=["django"])
        assert result == RefactorClass.REFACTOR_CLASS

    def test_path_with_special_chars(self):
        f = _finding(path="src/my-module/test_app.py", rule="ruff-c408")
        result = classify_finding(f)
        assert result == RefactorClass.SIMPLE_FIX

    def test_path_case_sensitivity(self):
        rule = get_context_rule("ruff-c408")
        ctx_lower = match_context("zerver/test_views.py", rule)
        ctx_upper = match_context("zerver/Test_Views.py", rule)
        assert ctx_lower is not None
        assert ctx_upper is None or isinstance(ctx_upper, ContextOverride)


class TestContextRulesDataIntegrity:
    def test_all_context_rules_have_required_fields(self):
        for entry in CONTEXT_RULES:
            assert "rule" in entry, f"Missing 'rule' in {entry}"
            assert "default_strategy" in entry, f"Missing 'default_strategy' in {entry}"
            assert "contexts" in entry, f"Missing 'contexts' in {entry}"
            assert isinstance(entry["contexts"], list)

    def test_all_contexts_have_required_fields(self):
        for entry in CONTEXT_RULES:
            for ctx in entry["contexts"]:
                assert "file_patterns" in ctx, (
                    f"Missing 'file_patterns' in context of {entry['rule']}"
                )
                assert "framework" in ctx, (
                    f"Missing 'framework' in context of {entry['rule']}"
                )
                assert "fix_strategy" in ctx, (
                    f"Missing 'fix_strategy' in context of {entry['rule']}"
                )
                assert isinstance(ctx["file_patterns"], list)
                assert len(ctx["file_patterns"]) >= 1

    def test_all_fix_strategies_valid(self):
        valid = {"skip", "deterministic", "deterministic_safe", "llm_with_context"}
        for entry in CONTEXT_RULES:
            assert entry["default_strategy"] in valid, (
                f"Invalid default_strategy '{entry['default_strategy']}' for {entry['rule']}"
            )
            for ctx in entry["contexts"]:
                assert ctx["fix_strategy"] in valid, (
                    f"Invalid fix_strategy '{ctx['fix_strategy']}' in context of {entry['rule']}"
                )

    def test_all_frameworks_valid(self):
        valid = {"any", "django", "nextjs", "express"}
        for entry in CONTEXT_RULES:
            for ctx in entry["contexts"]:
                assert ctx["framework"] in valid, (
                    f"Invalid framework '{ctx['framework']}' in context of {entry['rule']}"
                )

    def test_no_duplicate_rule_entries(self):
        rules = [entry["rule"] for entry in CONTEXT_RULES]
        assert len(rules) == len(set(rules)), (
            f"Duplicate rules found: {[r for r in rules if rules.count(r) > 1]}"
        )

    def test_all_rule_names_are_strings(self):
        for entry in CONTEXT_RULES:
            assert isinstance(entry["rule"], str)
            for ctx in entry["contexts"]:
                for pattern in ctx["file_patterns"]:
                    assert isinstance(pattern, str), (
                        f"Non-string pattern '{pattern}' in {entry['rule']}"
                    )

    def test_prompt_hints_are_strings_when_present(self):
        for entry in CONTEXT_RULES:
            for ctx in entry["contexts"]:
                if "prompt_hint" in ctx and ctx["prompt_hint"] is not None:
                    assert isinstance(ctx["prompt_hint"], str)
                    assert len(ctx["prompt_hint"]) > 0
