#!/usr/bin/env python3
"""Tests for expanded context rules and framework detection.

Covers:
1. Rule count assertion
2. Framework detection (detect_frameworks)
3. Context routing (match_context) for all new rules
4. Framework filtering
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from bluei.engine.constants import CONTEXT_RULES
from bluei.engine.reforge import (
    ContextOverride,
    RefactorClass,
    classify_finding,
    get_context_rule,
    match_context,
)
from bluei.app.onboarding import detect_frameworks


# ── 1. Rule count ──────────────────────────────────────────────────────────


class TestRuleCount:
    def test_context_rules_count(self):
        assert len(CONTEXT_RULES) >= 28, (
            f"Expected >= 28 rules, got {len(CONTEXT_RULES)}"
        )


# ── 2. Framework detection ─────────────────────────────────────────────────


class TestFrameworkDetection:
    def test_detect_django_via_manage_py(self, tmp_path):
        (tmp_path / "manage.py").write_text("")
        assert "django" in detect_frameworks(tmp_path, "python")

    def test_detect_django_via_settings(self, tmp_path):
        pkg = tmp_path / "myproject"
        pkg.mkdir()
        (pkg / "settings.py").write_text("")
        assert "django" in detect_frameworks(tmp_path, "python")

    def test_detect_fastapi(self, tmp_path):
        (tmp_path / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n"
        )
        assert "fastapi" in detect_frameworks(tmp_path, "python")

    def test_detect_flask(self, tmp_path):
        (tmp_path / "app.py").write_text(
            "from flask import Flask\napp = Flask(__name__)\n"
        )
        assert "flask" in detect_frameworks(tmp_path, "python")

    def test_detect_celery_via_import(self, tmp_path):
        (tmp_path / "celery_app.py").write_text(
            "from celery import Celery\napp = Celery()\n"
        )
        assert "celery" in detect_frameworks(tmp_path, "python")

    def test_detect_celery_via_requirements(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("celery==5.3.0\nredis==4.0\n")
        assert "celery" in detect_frameworks(tmp_path, "python")

    def test_detect_nextjs(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"next": "^14.0.0"}})
        )
        assert "nextjs" in detect_frameworks(tmp_path, "typescript")

    def test_detect_express(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"express": "^4.18.0"}})
        )
        assert "express" in detect_frameworks(tmp_path, "javascript")

    def test_detect_react(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"react": "^18.0.0"}})
        )
        assert "react" in detect_frameworks(tmp_path, "javascript")

    def test_detect_nestjs(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"dependencies": {"@nestjs/core": "^10.0.0"}})
        )
        assert "nestjs" in detect_frameworks(tmp_path, "typescript")

    def test_detect_vitest(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"devDependencies": {"vitest": "^1.0.0"}})
        )
        assert "vitest" in detect_frameworks(tmp_path, "typescript")

    def test_no_framework(self, tmp_path):
        assert detect_frameworks(tmp_path, "python") == []

    def test_no_framework_empty_ts(self, tmp_path):
        assert detect_frameworks(tmp_path, "typescript") == []

    def test_multiple_frameworks(self, tmp_path):
        (tmp_path / "manage.py").write_text("")
        (tmp_path / "tasks.py").write_text(
            "from celery import Celery\napp = Celery()\n"
        )
        frameworks = detect_frameworks(tmp_path, "python")
        assert "django" in frameworks
        assert "celery" in frameworks

    def test_unknown_language(self, tmp_path):
        assert detect_frameworks(tmp_path, "unknown") == []


# ── 3. Context routing tests ───────────────────────────────────────────────


class TestContextRouting:
    """Parametrized tests for context rule routing."""

    @pytest.mark.parametrize(
        "rule,file_path,frameworks,expected_strategy",
        [
            # ruff-e501
            ("ruff-e501", "app/migrations/0001_initial.py", ["django"], "skip"),
            ("ruff-e501", "app/urls.py", [], "skip"),
            ("ruff-e501", "app/routing.py", [], "skip"),
            ("ruff-e501", "app/views.py", [], None),
            # broad-except
            ("broad-except", "src/tasks/worker.py", [], "skip"),
            ("broad-except", "src/celery_app.py", [], "skip"),
            ("broad-except", "src/workers/email.py", [], "skip"),
            ("broad-except", "src/middleware.py", ["django"], "skip"),
            ("broad-except", "tests/test_foo.py", [], "skip"),
            ("broad-except", "src/utils.py", [], None),
            # perf-pop-front-loop
            (
                "perf-pop-front-loop",
                "app/migrations/0002_add_field.py",
                ["django"],
                "skip",
            ),
            ("perf-pop-front-loop", "src/queue.py", [], None),
            # perf-list-membership-loop
            ("perf-list-membership-loop", "tests/test_perf.py", [], "skip"),
            ("perf-list-membership-loop", "src/lookup.py", [], None),
            # trailing-whitespace
            ("trailing-whitespace", "docs/guide.md", [], "skip"),
            ("trailing-whitespace", "docs/guide.rst", [], "skip"),
            ("trailing-whitespace", "notes.txt", [], "skip"),
            ("trailing-whitespace", "src/main.py", [], None),
            # hardcoded-tmp-path
            ("hardcoded-tmp-path", "tests/test_io.py", [], "skip"),
            ("hardcoded-tmp-path", "tests/conftest.py", [], "skip"),
            ("hardcoded-tmp-path", "scripts/setup.py", [], "llm_with_context"),
            ("hardcoded-tmp-path", "tools/gen_fixtures.py", [], "llm_with_context"),
            ("hardcoded-tmp-path", "src/utils.py", [], None),
            # type-explicit-any
            ("type-explicit-any", "src/types/global.d.ts", [], "skip"),
            ("type-explicit-any", "src/typings/custom.d.ts", [], "skip"),
            ("type-explicit-any", "tests/foo.test.ts", [], "deterministic_safe"),
            (
                "type-explicit-any",
                "src/__tests__/bar.spec.ts",
                [],
                "deterministic_safe",
            ),
            ("type-explicit-any", "db/migrations/001_init.ts", [], "skip"),
            ("type-explicit-any", "project/scripts/seed.ts", [], "skip"),
            ("type-explicit-any", "next.config.js", [], "skip"),
            ("type-explicit-any", "webpack.config.ts", [], "skip"),
            ("type-explicit-any", "vite.config.ts", [], "skip"),
            ("type-explicit-any", "rollup.config.mjs", [], "skip"),
            ("type-explicit-any", "src/utils.ts", [], None),
            # type-missing-return
            (
                "type-missing-return",
                "src/pages/api/handler.ts",
                ["nextjs"],
                "llm_with_context",
            ),
            (
                "type-missing-return",
                "src/app/api/route.ts",
                ["nextjs"],
                "llm_with_context",
            ),
            (
                "type-missing-return",
                "src/controllers/user.ts",
                ["express"],
                "llm_with_context",
            ),
            (
                "type-missing-return",
                "src/handlers/auth.ts",
                ["express"],
                "llm_with_context",
            ),
            ("type-missing-return", "src/utils.ts", [], None),
            ("type-missing-return", "src/pages/api/handler.ts", [], None),
            ("type-missing-return", "src/pages/api/handler.ts", ["react"], None),
            # xo-no-warning-comments
            ("xo-no-warning-comments", "src/test/foo.test.ts", [], "skip"),
            ("xo-no-warning-comments", "src/__tests__/bar.test.ts", [], "skip"),
            ("xo-no-warning-comments", "src/main.ts", [], None),
            # test-gap-missing-file
            ("test-gap-missing-file", "db/migrations/001.py", [], "skip"),
            ("test-gap-missing-file", "project/alembic/versions/abc.py", [], "skip"),
            ("test-gap-missing-file", "src/__pycache__/mod.py", [], "skip"),
            (
                "test-gap-missing-file",
                "project/node_modules/lodash/index.js",
                [],
                "skip",
            ),
            ("test-gap-missing-file", "project/.venv/lib/site.py", [], "skip"),
            ("test-gap-missing-file", "src/utils.py", [], None),
            # test-coverage-line
            ("test-coverage-line", "db/migrations/001.py", [], "skip"),
            ("test-coverage-line", "project/alembic/versions/abc.py", [], "skip"),
            ("test-coverage-line", "src/generated/api.ts", [], "skip"),
            ("test-coverage-line", "tests/conftest.py", [], "skip"),
            ("test-coverage-line", "tests/fixtures/common.py", [], "skip"),
            ("test-coverage-line", "src/utils.py", [], None),
            # orders-tax-truncation
            ("orders-tax-truncation", "tests/test_orders.py", [], "skip"),
            ("orders-tax-truncation", "tests/test_tax.py", [], "skip"),
            ("orders-tax-truncation", "src/orders.py", [], None),
            ("orders-tax-truncation", "src/services/orders.py", [], None),
            # debug-console-log
            ("debug-console-log", "src/foo.test.ts", [], "skip"),
            ("debug-console-log", "src/bar.spec.ts", [], "skip"),
            ("debug-console-log", "src/__tests__/baz.ts", [], "skip"),
            ("debug-console-log", "src/test/helper.ts", [], "skip"),
            ("debug-console-log", "scripts/build.ts", [], "skip"),
            ("debug-console-log", "tools/generate.ts", [], "skip"),
            ("debug-console-log", "src/debug/logger.ts", [], "skip"),
            ("debug-console-log", "src/app.ts", [], None),
            # go-empty-interface
            ("go-empty-interface", "vendor/golib/types.go", [], "skip"),
            ("go-empty-interface", "third_party/proto/types.go", [], "skip"),
            ("go-empty-interface", "api/users.generated.go", [], "skip"),
            ("go-empty-interface", "src/types.go", [], None),
            # rust-unwrap-panic
            ("rust-unwrap-panic", "src/test/integration.rs", [], "skip"),
            ("rust-unwrap-panic", "src/lib_test.rs", [], "skip"),
            ("rust-unwrap-panic", "tests/common/mod.rs", [], "skip"),
            ("rust-unwrap-panic", "src/main.rs", [], None),
            # ts-unsafe-any-cast
            ("ts-unsafe-any-cast", "src/types/global.d.ts", [], "skip"),
            ("ts-unsafe-any-cast", "src/typings/env.d.ts", [], "skip"),
            ("ts-unsafe-any-cast", "src/foo.test.ts", [], "deterministic_safe"),
            ("ts-unsafe-any-cast", "src/bar.spec.ts", [], "deterministic_safe"),
            ("ts-unsafe-any-cast", "webpack.config.ts", [], "skip"),
            ("ts-unsafe-any-cast", "vite.config.ts", [], "skip"),
            ("ts-unsafe-any-cast", "next.config.mjs", [], "skip"),
            ("ts-unsafe-any-cast", "src/utils.ts", [], None),
            # go-unchecked-error
            ("go-unchecked-error", "src/handler_test.go", [], "skip"),
            ("go-unchecked-error", "src/test/helper.go", [], "skip"),
            ("go-unchecked-error", "vendor/golib/client.go", [], "skip"),
            ("go-unchecked-error", "api/generated.types.go", [], None),
            ("go-unchecked-error", "src/handler.go", [], None),
            # ts-unhandled-promise
            ("ts-unhandled-promise", "src/foo.test.ts", [], "skip"),
            ("ts-unhandled-promise", "src/bar.spec.ts", [], "skip"),
            ("ts-unhandled-promise", "src/__tests__/setup.ts", [], "skip"),
            ("ts-unhandled-promise", "webpack.config.ts", [], "skip"),
            ("ts-unhandled-promise", "vite.config.ts", [], "skip"),
            ("ts-unhandled-promise", "src/app.ts", [], None),
            # go-goroutine-unsafe
            ("go-goroutine-unsafe", "src/worker_test.go", [], "skip"),
            ("go-goroutine-unsafe", "src/test/integration.go", [], "skip"),
            ("go-goroutine-unsafe", "vendor/golib/pool.go", [], "skip"),
            ("go-goroutine-unsafe", "src/worker.go", [], None),
            # type-missing-param
            ("type-missing-param", "src/types/env.d.ts", [], "skip"),
            ("type-missing-param", "src/types/api.d.ts", [], "skip"),
            ("type-missing-param", "next.config.ts", [], "skip"),
            ("type-missing-param", "src/generated/api.ts", [], "skip"),
            ("type-missing-param", "src/auto-generated/schema.ts", [], "skip"),
            ("type-missing-param", "src/utils.ts", [], None),
            # type-untyped-import
            ("type-untyped-import", "next.config.ts", [], "skip"),
            ("type-untyped-import", "src/generated/proto.ts", [], "skip"),
            ("type-untyped-import", "scripts/deploy.ts", [], "skip"),
            ("type-untyped-import", "src/utils.ts", [], None),
            # rust-expect-vague
            ("rust-expect-vague", "src/generated/types.rs", [], "skip"),
            ("rust-expect-vague", "src/auto-generated/mod.rs", [], "skip"),
            ("rust-expect-vague", "src/lib_test.rs", [], "skip"),
            ("rust-expect-vague", "tests/integration.rs", [], "skip"),
            ("rust-expect-vague", "src/main.rs", [], None),
        ],
    )
    def test_context_routing(self, rule, file_path, frameworks, expected_strategy):
        context_rule = get_context_rule(rule)
        assert context_rule is not None, f"No context rule found for {rule}"
        matched = match_context(file_path, context_rule, detected_frameworks=frameworks)
        if expected_strategy is None:
            assert matched is None, (
                f"Expected no match for {rule} + {file_path}, got {matched.fix_strategy}"
            )
        else:
            assert matched is not None, (
                f"Expected match for {rule} + {file_path}, got None"
            )
            assert matched.fix_strategy == expected_strategy, (
                f"Expected {expected_strategy} for {rule} + {file_path}, got {matched.fix_strategy}"
            )


class TestClassifyFindingWithFrameworks:
    """Test classify_finding integration with detected_frameworks."""

    def test_type_explicit_any_in_dts_skipped(self, make_finding):
        finding = make_finding(
            path="src/types/global.d.ts",
            rule="type-explicit-any",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # skip → REFACTOR_CLASS

    def test_type_explicit_any_in_source_default(self, make_finding):
        finding = make_finding(
            path="src/utils.ts",
            rule="type-explicit-any",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.CONTEXTUAL_FIX  # default llm_with_context

    def test_type_explicit_any_in_test_deterministic(self, make_finding):
        finding = make_finding(
            path="tests/foo.test.ts",
            rule="type-explicit-any",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.SIMPLE_FIX  # deterministic_safe

    def test_type_missing_return_nextjs(self, make_finding):
        finding = make_finding(
            path="src/pages/api/handler.ts",
            rule="type-missing-return",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding, detected_frameworks=["nextjs"])
        assert rc == RefactorClass.CONTEXTUAL_FIX  # llm_with_context

    def test_type_missing_return_no_framework(self, make_finding):
        finding = make_finding(
            path="src/pages/api/handler.ts",
            rule="type-missing-return",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.SIMPLE_FIX  # default deterministic_safe

    def test_test_gap_missing_file_in_migrations(self, make_finding):
        finding = make_finding(
            path="db/migrations/001.py",
            rule="test-gap-missing-file",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # skip

    def test_test_gap_missing_file_in_source(self, make_finding):
        finding = make_finding(
            path="src/utils.py",
            rule="test-gap-missing-file",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.SIMPLE_FIX  # default deterministic_safe

    def test_broad_except_in_task_worker(self, make_finding):
        finding = make_finding(
            path="src/tasks/worker.py",
            rule="broad-except",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # skip

    def test_broad_except_in_django_middleware(self, make_finding):
        finding = make_finding(
            path="src/middleware.py",
            rule="broad-except",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding, detected_frameworks=["django"])
        assert rc == RefactorClass.REFACTOR_CLASS  # skip

    def test_broad_except_in_source(self, make_finding):
        finding = make_finding(
            path="src/utils.py",
            rule="broad-except",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.CONTEXTUAL_FIX  # default llm_with_context

    def test_trailing_whitespace_in_markdown(self, make_finding):
        finding = make_finding(
            path="docs/guide.md",
            rule="trailing-whitespace",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # skip

    def test_trailing_whitespace_in_source(self, make_finding):
        finding = make_finding(
            path="src/main.py",
            rule="trailing-whitespace",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.SIMPLE_FIX  # default deterministic_safe

    def test_hardcoded_tmp_in_test(self, make_finding):
        finding = make_finding(
            path="tests/test_io.py",
            rule="hardcoded-tmp-path",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # skip

    def test_hardcoded_tmp_in_script(self, make_finding):
        finding = make_finding(
            path="scripts/setup.py",
            rule="hardcoded-tmp-path",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.CONTEXTUAL_FIX  # llm_with_context

    def test_hardcoded_tmp_in_source(self, make_finding):
        finding = make_finding(
            path="src/utils.py",
            rule="hardcoded-tmp-path",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.SIMPLE_FIX  # default deterministic_safe

    def test_xo_max_lines_still_refactor_class(self, make_finding):
        finding = make_finding(
            path="src/hooks.ts",
            rule="xo-max-lines",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # blocked by REFACTOR_CLASS_RULES

    def test_xo_complexity_still_refactor_class(self, make_finding):
        finding = make_finding(
            path="src/parser.ts",
            rule="xo-complexity",
            safe_to_autofix=False,
            quick_win=False,
        )
        rc = classify_finding(finding)
        assert rc == RefactorClass.REFACTOR_CLASS  # blocked by REFACTOR_CLASS_RULES


class TestFrameworkFiltering:
    """Test that framework-specific contexts only match when framework is detected."""

    def test_django_context_requires_django_framework(self):
        rule = get_context_rule("ruff-c408")
        assert rule is not None
        result = match_context(
            "app/migrations/0001.py", rule, detected_frameworks=["flask"]
        )
        assert result is None

    def test_django_context_matches_with_django_framework(self):
        rule = get_context_rule("ruff-c408")
        assert rule is not None
        result = match_context(
            "app/migrations/0001.py", rule, detected_frameworks=["django"]
        )
        assert result is not None
        assert result.fix_strategy == "skip"

    def test_any_framework_matches_without_frameworks(self):
        rule = get_context_rule("ruff-c408")
        assert rule is not None
        result = match_context("tests/test_views.py", rule, detected_frameworks=None)
        assert result is not None
        assert result.fix_strategy == "deterministic_safe"

    def test_nextjs_context_requires_nextjs(self):
        rule = get_context_rule("type-missing-return")
        assert rule is not None
        result = match_context(
            "src/pages/api/handler.ts", rule, detected_frameworks=["react"]
        )
        assert result is None

    def test_express_context_requires_express(self):
        rule = get_context_rule("type-missing-return")
        assert rule is not None
        result = match_context(
            "src/controllers/user.ts", rule, detected_frameworks=["nestjs"]
        )
        assert result is None


class TestNewContextRuleClassifyFinding:
    """classify_finding() end-to-end tests for the 11 new context rules."""

    # Tier 1: Protect deterministic recipes

    def test_orders_tax_truncation_in_source(self, make_finding):
        f = make_finding(
            path="src/orders.py",
            rule="orders-tax-truncation",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.SIMPLE_FIX

    def test_orders_tax_truncation_in_test(self, make_finding):
        f = make_finding(
            path="tests/test_orders.py",
            rule="orders-tax-truncation",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_debug_console_log_in_source(self, make_finding):
        f = make_finding(
            path="src/app.ts",
            rule="debug-console-log",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.SIMPLE_FIX

    def test_debug_console_log_in_test(self, make_finding):
        f = make_finding(
            path="src/foo.test.ts",
            rule="debug-console-log",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_debug_console_log_in_scripts(self, make_finding):
        f = make_finding(
            path="scripts/build.ts",
            rule="debug-console-log",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_go_empty_interface_in_source(self, make_finding):
        f = make_finding(
            path="src/types.go",
            rule="go-empty-interface",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.SIMPLE_FIX

    def test_go_empty_interface_in_vendor(self, make_finding):
        f = make_finding(
            path="vendor/golib/types.go",
            rule="go-empty-interface",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    # Tier 2: Real false-positive patterns

    def test_rust_unwrap_panic_in_source(self, make_finding):
        f = make_finding(
            path="src/main.rs",
            rule="rust-unwrap-panic",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_rust_unwrap_panic_in_test(self, make_finding):
        f = make_finding(
            path="src/lib_test.rs",
            rule="rust-unwrap-panic",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_ts_unsafe_any_cast_in_source(self, make_finding):
        f = make_finding(
            path="src/utils.ts",
            rule="ts-unsafe-any-cast",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_ts_unsafe_any_cast_in_dts(self, make_finding):
        f = make_finding(
            path="src/types/global.d.ts",
            rule="ts-unsafe-any-cast",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_ts_unsafe_any_cast_in_test(self, make_finding):
        f = make_finding(
            path="src/foo.test.ts",
            rule="ts-unsafe-any-cast",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.SIMPLE_FIX

    def test_ts_unsafe_any_cast_in_config(self, make_finding):
        f = make_finding(
            path="webpack.config.ts",
            rule="ts-unsafe-any-cast",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_go_unchecked_error_in_source(self, make_finding):
        f = make_finding(
            path="src/handler.go",
            rule="go-unchecked-error",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_go_unchecked_error_in_test(self, make_finding):
        f = make_finding(
            path="src/handler_test.go",
            rule="go-unchecked-error",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_go_unchecked_error_in_vendor(self, make_finding):
        f = make_finding(
            path="vendor/golib/client.go",
            rule="go-unchecked-error",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_ts_unhandled_promise_in_source(self, make_finding):
        f = make_finding(
            path="src/app.ts",
            rule="ts-unhandled-promise",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_ts_unhandled_promise_in_test(self, make_finding):
        f = make_finding(
            path="src/foo.test.ts",
            rule="ts-unhandled-promise",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_ts_unhandled_promise_in_config(self, make_finding):
        f = make_finding(
            path="vite.config.ts",
            rule="ts-unhandled-promise",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_go_goroutine_unsafe_in_source(self, make_finding):
        f = make_finding(
            path="src/worker.go",
            rule="go-goroutine-unsafe",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_go_goroutine_unsafe_in_test(self, make_finding):
        f = make_finding(
            path="src/worker_test.go",
            rule="go-goroutine-unsafe",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_go_goroutine_unsafe_in_vendor(self, make_finding):
        f = make_finding(
            path="vendor/golib/pool.go",
            rule="go-goroutine-unsafe",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    # Tier 3: Reasonable

    def test_type_missing_param_in_source(self, make_finding):
        f = make_finding(
            path="src/utils.ts",
            rule="type-missing-param",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_type_missing_param_in_dts(self, make_finding):
        f = make_finding(
            path="src/types/env.d.ts",
            rule="type-missing-param",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_type_missing_param_in_config(self, make_finding):
        f = make_finding(
            path="next.config.ts",
            rule="type-missing-param",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_type_untyped_import_in_source(self, make_finding):
        f = make_finding(
            path="src/utils.ts",
            rule="type-untyped-import",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_type_untyped_import_in_config(self, make_finding):
        f = make_finding(
            path="next.config.ts",
            rule="type-untyped-import",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_type_untyped_import_in_scripts(self, make_finding):
        f = make_finding(
            path="scripts/deploy.ts",
            rule="type-untyped-import",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_rust_expect_vague_in_source(self, make_finding):
        f = make_finding(
            path="src/main.rs",
            rule="rust-expect-vague",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.CONTEXTUAL_FIX

    def test_rust_expect_vague_in_generated(self, make_finding):
        f = make_finding(
            path="src/generated/types.rs",
            rule="rust-expect-vague",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS

    def test_rust_expect_vague_in_test(self, make_finding):
        f = make_finding(
            path="src/lib_test.rs",
            rule="rust-expect-vague",
            safe_to_autofix=False,
            quick_win=False,
        )
        assert classify_finding(f) == RefactorClass.REFACTOR_CLASS


class TestNewContextRuleIntegrity:
    """Data integrity checks for new context rules."""

    @pytest.mark.parametrize(
        "rule_name",
        [
            "orders-tax-truncation",
            "debug-console-log",
            "go-empty-interface",
            "rust-unwrap-panic",
            "ts-unsafe-any-cast",
            "go-unchecked-error",
            "ts-unhandled-promise",
            "go-goroutine-unsafe",
            "type-missing-param",
            "type-untyped-import",
            "rust-expect-vague",
        ],
    )
    def test_rule_has_valid_entry(self, rule_name):
        rule = get_context_rule(rule_name)
        assert rule is not None, f"No context rule for {rule_name}"
        assert rule.default_strategy in (
            "deterministic_safe",
            "llm_with_context",
            "skip",
            "deterministic",
        )
        assert len(rule.contexts) > 0

    @pytest.mark.parametrize(
        "rule_name",
        [
            "orders-tax-truncation",
            "debug-console-log",
            "go-empty-interface",
            "rust-unwrap-panic",
            "ts-unsafe-any-cast",
            "go-unchecked-error",
            "ts-unhandled-promise",
            "go-goroutine-unsafe",
            "type-missing-param",
            "type-untyped-import",
            "rust-expect-vague",
        ],
    )
    def test_all_contexts_use_any_framework(self, rule_name):
        rule = get_context_rule(rule_name)
        assert rule is not None
        for ctx in rule.contexts:
            assert ctx.framework == "any", (
                f"{rule_name} context '{ctx.file_patterns}' uses framework={ctx.framework}, expected 'any'"
            )

    def test_no_duplicate_rules(self):
        rule_names = [r["rule"] for r in CONTEXT_RULES]
        assert len(rule_names) == len(set(rule_names)), (
            "Duplicate rule names in CONTEXT_RULES"
        )

    def test_generated_go_pattern_matches(self):
        rule = get_context_rule("go-empty-interface")
        assert rule is not None
        matched = match_context("api/users.generated.go", rule, detected_frameworks=[])
        assert matched is not None
        assert matched.fix_strategy == "skip"

    def test_deeply_nested_vendor_go(self):
        rule = get_context_rule("go-empty-interface")
        assert rule is not None
        matched = match_context(
            "src/vendor/google/api/types.go", rule, detected_frameworks=[]
        )
        assert matched is not None
        assert matched.fix_strategy == "skip"

    def test_deeply_nested_generated_rust(self):
        rule = get_context_rule("rust-expect-vague")
        assert rule is not None
        matched = match_context(
            "src/proto/generated/messages.rs", rule, detected_frameworks=[]
        )
        assert matched is not None
        assert matched.fix_strategy == "skip"
