import ast
from pathlib import Path

import pytest

from bluei.engine.ast_engine.context import (
    build_parent_map,
    extract_function_calls,
    extract_method_chain,
    extract_variable_names,
    find_enclosing_function,
    find_enclosing_loop,
    find_parent,
    function_has_isinstance_guard,
    is_in_except_handler,
    is_in_return_statement,
)
from bluei.engine.ast_engine.matcher import ASTPatternMatcher
from bluei.engine.ast_engine.models import (
    ASTMatch,
    ASTPattern,
    ASTTransform,
    ASTTransformResult,
)
from bluei.engine.ast_engine.patterns.python_patterns import (
    PYTHON_PATTERNS,
    get_patterns,
)
from bluei.engine.ast_engine.transformer import ASTTransformer
from bluei.engine.ast_engine.transforms.python_transforms import (
    PYTHON_TRANSFORMS,
    get_transforms,
)


def _match(source, rule_id=None):
    matcher = ASTPatternMatcher(PYTHON_PATTERNS)
    matches = matcher.find_matches(source, "test.py", "python")
    if rule_id:
        return [m for m in matches if m.pattern.id == rule_id]
    return matches


def _transform(source, rule_id):
    matcher = ASTPatternMatcher(PYTHON_PATTERNS)
    transformer = ASTTransformer(PYTHON_TRANSFORMS)
    matches = matcher.find_matches(source, "test.py", "python")
    results = []
    for m in matches:
        if m.pattern.id == rule_id:
            t = transformer.get_transform(rule_id)
            if t:
                results.append(transformer.apply(source, m, t))
    return results


# ── Models ──


class TestModels:
    def test_ast_pattern_fields(self):
        p = ASTPattern(
            id="test-pattern",
            language="python",
            node_type="BinOp",
            constraints={"op": "Add"},
            confidence=0.9,
            category="bug",
        )
        assert p.id == "test-pattern"
        assert p.language == "python"
        assert p.confidence == 0.9
        assert p.negation_constraints is None

    def test_ast_match_fields(self):
        p = ASTPattern(id="t", language="python", node_type="BinOp")
        m = ASTMatch(pattern=p, node=None, line=5, col=0, source_text="a + b")
        assert m.line == 5
        assert m.source_text == "a + b"

    def test_ast_transform_fields(self):
        t = ASTTransform(
            id="fix-t",
            source_pattern_id="t",
            transform_type="replace_op",
            params={"old_op": "Add", "new_op": "Sub"},
        )
        assert t.transform_type == "replace_op"

    def test_ast_transform_result(self):
        r = ASTTransformResult(success=True, source="x - y", transform_id="fix-t")
        assert r.success
        assert r.error is None


# ── Context Helpers ──


class TestContextHelpers:
    def test_build_parent_map(self):
        tree = ast.parse("x = 1 + 2")
        pmap = build_parent_map(tree)
        assert len(pmap) > 0

    def test_find_parent(self):
        tree = ast.parse("x = 1 + 2")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                parent = find_parent(node, pmap)
                assert parent is not None
                assert isinstance(parent, ast.Assign)
                break

    def test_find_enclosing_function(self):
        tree = ast.parse("def foo():\n    return 1 + 2\n")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                func = find_enclosing_function(node, pmap)
                assert func is not None
                assert func.name == "foo"
                break

    def test_find_enclosing_function_none(self):
        tree = ast.parse("x = 1 + 2")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                func = find_enclosing_function(node, pmap)
                assert func is None
                break

    def test_find_enclosing_loop(self):
        tree = ast.parse("for x in items:\n    y = x + 1\n")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                loop = find_enclosing_loop(node, pmap)
                assert loop is not None
                assert isinstance(loop, ast.For)
                break

    def test_find_enclosing_loop_none(self):
        tree = ast.parse("x = 1 + 2")
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                loop = find_enclosing_loop(node, pmap)
                assert loop is None
                break

    def test_extract_variable_names(self):
        tree = ast.parse("x = a + b")
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                names = extract_variable_names(node)
                assert "a" in names
                assert "b" in names
                break

    def test_extract_method_chain(self):
        source = "value.strip().lower()"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "lower":
                    chain = extract_method_chain(node)
                    assert "lower" in chain
                    assert "strip" in chain
                    break

    def test_extract_function_calls(self):
        source = "def foo():\n    bar()\n    baz.strip()\n"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "foo":
                calls = extract_function_calls(node)
                assert "bar" in calls
                assert "strip" in calls
                break

    def test_is_in_return_statement(self):
        source = "def foo():\n    return value.lower()\n"
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "lower":
                    assert is_in_return_statement(node, pmap)
                    break

    def test_is_in_except_handler(self):
        source = "try:\n    pass\nexcept Exception:\n    raise\n"
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise):
                assert is_in_except_handler(node, pmap)
                break

    def test_function_has_isinstance_guard(self):
        source = "def foo(x):\n    if isinstance(x, str):\n        return x.lower()\n"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "foo":
                assert function_has_isinstance_guard(node)
                break

    def test_function_no_isinstance_guard(self):
        source = "def foo(x):\n    return x.lower()\n"
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "foo":
                assert not function_has_isinstance_guard(node)
                break


# ── Pattern Matching ──


class TestPerfPopFrontLoop:
    def test_match_pop_zero_in_while(self):
        source = "def f(items):\n    while items:\n        current = items.pop(0)\n"
        matches = _match(source, "perf-pop-front-loop")
        assert len(matches) == 1
        assert matches[0].line == 3

    def test_match_pop_zero_in_for(self):
        source = "def f(items):\n    for _ in range(10):\n        x = items.pop(0)\n"
        matches = _match(source, "perf-pop-front-loop")
        assert len(matches) == 1

    def test_no_match_pop_zero_not_in_loop(self):
        source = "x = items.pop(0)\n"
        matches = _match(source, "perf-pop-front-loop")
        assert len(matches) == 0

    def test_no_match_pop_nonzero(self):
        source = "for _ in range(10):\n    x = items.pop(1)\n"
        matches = _match(source, "perf-pop-front-loop")
        assert len(matches) == 0

    def test_no_match_pop_no_args(self):
        source = "for _ in range(10):\n    x = items.pop()\n"
        matches = _match(source, "perf-pop-front-loop")
        assert len(matches) == 0

    def test_generic_variable_names(self):
        source = "while queue:\n    task = queue.pop(0)\n    process(task)\n"
        matches = _match(source, "perf-pop-front-loop")
        assert len(matches) == 1


class TestPerfListMembershipLoop:
    def test_match_in_loop(self):
        source = "for user in users:\n    if user in banned:\n        break\n"
        matches = _match(source, "perf-list-membership-loop")
        assert len(matches) == 1

    def test_no_match_not_in_loop(self):
        source = "if user in banned:\n    pass\n"
        matches = _match(source, "perf-list-membership-loop")
        assert len(matches) == 0

    def test_generic_names(self):
        source = (
            "for item in collection:\n    if item in exclude_list:\n        continue\n"
        )
        matches = _match(source, "perf-list-membership-loop")
        assert len(matches) == 1


class TestNotificationsEmailNoTrim:
    def test_match_lower_without_strip(self):
        source = "def normalize_email(value):\n    return value.lower()\n"
        matches = _match(source, "notifications-email-no-trim")
        assert len(matches) == 1

    def test_no_match_lower_with_strip(self):
        source = "def normalize_email(value):\n    return value.strip().lower()\n"
        matches = _match(source, "notifications-email-no-trim")
        assert len(matches) == 0

    def test_no_match_lower_not_in_return(self):
        source = "def process(value):\n    x = value.lower()\n    return x\n"
        matches = _match(source, "notifications-email-no-trim")
        assert len(matches) == 0

    def test_generic_function_name(self):
        source = "def clean(text):\n    return text.lower()\n"
        matches = _match(source, "notifications-email-no-trim")
        assert len(matches) == 1


class TestNotificationsTypeGuardMissing:
    def test_match_function_with_str_methods_no_guard(self):
        source = "def process(data):\n    return data.lower()\n"
        matches = _match(source, "notifications-type-guard-missing")
        assert len(matches) == 1

    def test_no_match_function_with_guard(self):
        source = "def process(data):\n    if isinstance(data, str):\n        return data.lower()\n"
        matches = _match(source, "notifications-type-guard-missing")
        assert len(matches) == 0

    def test_no_match_function_without_str_methods(self):
        source = "def process(data):\n    return data + 1\n"
        matches = _match(source, "notifications-type-guard-missing")
        assert len(matches) == 0


class TestHardcodedTmpPath:
    def test_match_path_tmp(self):
        source = (
            "from pathlib import Path\nconfig = Path('/tmp/qa-sandbox-state.json')\n"
        )
        matches = _match(source, "hardcoded-tmp-path")
        assert len(matches) == 1

    def test_match_tmp_string_constant(self):
        source = "log_path = '/tmp/app.log'\n"
        matches = _match(source, "hardcoded-tmp-path-string")
        assert len(matches) == 1

    def test_no_match_non_tmp_path(self):
        source = "from pathlib import Path\nconfig = Path('/var/lib/state.json')\n"
        matches = _match(source, "hardcoded-tmp-path")
        assert len(matches) == 0

    def test_no_match_non_tmp_string(self):
        source = "log_path = '/var/log/app.log'\n"
        matches = _match(source, "hardcoded-tmp-path-string")
        assert len(matches) == 0


class TestCatalogQueryNotNormalized:
    def test_match_eq_without_normalize(self):
        source = (
            "def find_item(items, query):\n    if item == query:\n        return item\n"
        )
        matches = _match(source, "catalog-query-not-normalized")
        assert len(matches) == 1

    def test_no_match_eq_with_strip(self):
        source = "def find_item(items, query):\n    if item.strip() == query.strip():\n        return item\n"
        matches = _match(source, "catalog-query-not-normalized")
        assert len(matches) == 0

    def test_no_match_eq_with_lower(self):
        source = "def find_item(items, query):\n    if item.lower() == query.lower():\n        return item\n"
        matches = _match(source, "catalog-query-not-normalized")
        assert len(matches) == 0

    def test_generic_variable_names(self):
        source = (
            "def search(records, key):\n    if record == key:\n        return record\n"
        )
        matches = _match(source, "catalog-query-not-normalized")
        assert len(matches) == 1


class TestDiscountMathSign:
    def test_match_add_in_discount_context(self):
        source = (
            "def calculate_discount(amount, discount):\n    return amount + discount\n"
        )
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 1

    def test_no_match_sub_in_discount_context(self):
        source = (
            "def calculate_discount(amount, discount):\n    return amount - discount\n"
        )
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 0

    def test_no_match_add_outside_discount(self):
        source = "def combine(a, b):\n    return a + b\n"
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 0

    def test_no_match_add_not_in_return(self):
        source = "def calculate_discount(amount, discount):\n    total = amount + discount\n    return total\n"
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 0

    def test_match_price_function(self):
        source = "def compute_price(base, rebate):\n    return base + rebate\n"
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 1

    def test_match_coupon_function(self):
        source = "def apply_coupon(cost, coupon):\n    return cost + coupon\n"
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 1


class TestBroadExcept:
    def test_match_except_exception(self):
        source = "try:\n    pass\nexcept Exception:\n    pass\n"
        matches = _match(source, "broad-except")
        assert len(matches) == 1

    def test_match_bare_except(self):
        source = "try:\n    pass\nexcept:\n    pass\n"
        matches = _match(source, "broad-except")
        assert len(matches) == 1

    def test_no_match_specific_exception(self):
        source = "try:\n    pass\nexcept ValueError:\n    pass\n"
        matches = _match(source, "broad-except")
        assert len(matches) == 0

    def test_no_match_tuple_exception(self):
        source = "try:\n    pass\nexcept (ValueError, TypeError):\n    pass\n"
        matches = _match(source, "broad-except")
        assert len(matches) == 0


class TestNoFalsePositivesFromComments:
    def test_comment_not_matched(self):
        source = "# return amount + discount\nresult = a - b\n"
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 0

    def test_string_literal_not_matched(self):
        source = "msg = 'return amount + discount'\nresult = a - b\n"
        matches = _match(source, "discount-math-sign")
        assert len(matches) == 0

    def test_comment_todo_not_matched_as_broad_except(self):
        source = "# except Exception: old code\nclass Foo:\n    pass\n"
        matches = _match(source, "broad-except")
        assert len(matches) == 0


class TestSyntaxErrorGraceful:
    def test_invalid_syntax_returns_empty(self):
        source = "def foo(:\n"
        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        matches = matcher.find_matches(source, "bad.py", "python")
        assert matches == []


# ── Transforms ──


class TestTransformReplaceOp:
    def test_add_to_sub(self):
        source = (
            "def calculate_discount(amount, discount):\n    return amount + discount\n"
        )
        results = _transform(source, "discount-math-sign")
        assert len(results) == 1
        assert results[0].success
        assert "-" in results[0].source
        assert "amount + discount" not in results[0].source

    def test_output_is_valid_python(self):
        source = (
            "def calculate_discount(amount, discount):\n    return amount + discount\n"
        )
        results = _transform(source, "discount-math-sign")
        assert results[0].success
        compile(results[0].source, "<test>", "exec")


class TestTransformPrependMethod:
    def test_prepend_strip(self):
        source = "def normalize_email(value):\n    return value.lower()\n"
        results = _transform(source, "notifications-email-no-trim")
        assert len(results) == 1
        assert results[0].success
        assert "value.strip().lower()" in results[0].source

    def test_output_is_valid_python(self):
        source = "def normalize_email(value):\n    return value.lower()\n"
        results = _transform(source, "notifications-email-no-trim")
        compile(results[0].source, "<test>", "exec")


class TestTransformWrapNormalize:
    def test_wrap_eq_operands(self):
        source = (
            "def find_item(items, query):\n    if item == query:\n        return item\n"
        )
        results = _transform(source, "catalog-query-not-normalized")
        assert len(results) == 1
        assert results[0].success
        assert "strip()" in results[0].source
        assert "lower()" in results[0].source

    def test_output_is_valid_python(self):
        source = (
            "def find_item(items, query):\n    if item == query:\n        return item\n"
        )
        results = _transform(source, "catalog-query-not-normalized")
        compile(results[0].source, "<test>", "exec")


class TestTransformWrapSet:
    def test_wrap_in_set(self):
        source = "for user in users:\n    if user in banned:\n        break\n"
        results = _transform(source, "perf-list-membership-loop")
        assert len(results) == 1
        assert results[0].success
        assert "set(banned)" in results[0].source

    def test_output_is_valid_python(self):
        source = "for user in users:\n    if user in banned:\n        break\n"
        results = _transform(source, "perf-list-membership-loop")
        compile(results[0].source, "<test>", "exec")


class TestTransformInsertGuard:
    def test_insert_isinstance_guard(self):
        source = "def process(data):\n    return data.lower()\n"
        results = _transform(source, "notifications-type-guard-missing")
        assert len(results) == 1
        assert results[0].success
        assert "isinstance" in results[0].source

    def test_output_is_valid_python(self):
        source = "def process(data):\n    return data.lower()\n"
        results = _transform(source, "notifications-type-guard-missing")
        compile(results[0].source, "<test>", "exec")


class TestTransformReplacePath:
    def test_replace_tmp_path(self):
        source = (
            "from pathlib import Path\nconfig = Path('/tmp/qa-sandbox-state.json')\n"
        )
        results = _transform(source, "hardcoded-tmp-path")
        assert len(results) == 1
        assert results[0].success
        assert "/var/lib/" in results[0].source
        assert "/tmp/" not in results[0].source


# ── Pattern Registry ──


class TestPatternRegistry:
    def test_get_patterns(self):
        patterns = get_patterns()
        assert len(patterns) == 9

    def test_get_transforms(self):
        transforms = get_transforms()
        assert len(transforms) == 8

    def test_all_patterns_have_language(self):
        for p in PYTHON_PATTERNS:
            assert p.language == "python"

    def test_all_patterns_have_id(self):
        ids = [p.id for p in PYTHON_PATTERNS]
        assert len(ids) == len(set(ids))

    def test_transforms_reference_valid_patterns(self):
        pattern_ids = {p.id for p in PYTHON_PATTERNS}
        for t in PYTHON_TRANSFORMS:
            assert t.source_pattern_id in pattern_ids


# ── Orchestrator Integration ──


class TestOrchestratorIntegration:
    def test_ast_scan_finds_findings(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files
        from bluei.engine.constants import DETECTOR_CATALOG

        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}

        src = tmp_path / "src"
        src.mkdir()
        (src / "calc.py").write_text(
            "def calculate_discount(amount, discount):\n    return amount + discount\n"
        )
        log_file = tmp_path / "test.log"
        log_file.write_text("")

        findings = _ast_scan_python_files(tmp_path, log_file, rule_meta)
        assert len(findings) >= 1
        assert any(f.rule == "discount-math-sign" for f in findings)

    def test_ast_scan_skips_pycache(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files
        from bluei.engine.constants import DETECTOR_CATALOG

        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}

        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "bad.py").write_text(
            "def calculate_discount(a, d):\n    return a + d\n"
        )
        log_file = tmp_path / "test.log"
        log_file.write_text("")

        findings = _ast_scan_python_files(tmp_path, log_file, rule_meta)
        discount = [f for f in findings if f.rule == "discount-math-sign"]
        assert len(discount) == 0

    def test_ast_scan_skips_empty_files(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files
        from bluei.engine.constants import DETECTOR_CATALOG

        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}

        (tmp_path / "empty.py").write_text("")
        log_file = tmp_path / "test.log"
        log_file.write_text("")

        findings = _ast_scan_python_files(tmp_path, log_file, rule_meta)
        assert len(findings) == 0

    def test_ast_scan_deduplicates(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files
        from bluei.engine.constants import DETECTOR_CATALOG

        rule_meta = {e["rule"]: e for e in DETECTOR_CATALOG}

        (tmp_path / "calc.py").write_text(
            "def calculate_discount(amount, discount):\n    return amount + discount\n"
        )
        log_file = tmp_path / "test.log"
        log_file.write_text("")

        findings = _ast_scan_python_files(tmp_path, log_file, rule_meta)
        rule_counts = {}
        for f in findings:
            rule_counts[f.rule] = rule_counts.get(f.rule, 0) + 1
        for rule, count in rule_counts.items():
            assert count == 1, f"Rule {rule} appeared {count} times"


# ── Constraint Coverage: left, right, func, ops, test, type, args ──


class TestCompareLeftConstraint:
    """Covers _check_constraint 'left' → _check_node_spec (line 155)."""

    def test_match_left_name_constraint(self):
        pat = ASTPattern(
            id="test-left-name",
            language="python",
            node_type="Compare",
            constraints={"left": {"type": "Name", "id": "x"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 10:\n    pass\n", "test.py", "python")
        assert len(matches) == 1

    def test_no_match_wrong_left_id(self):
        pat = ASTPattern(
            id="test-left-wrong",
            language="python",
            node_type="Compare",
            constraints={"left": {"type": "Name", "id": "y"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 10:\n    pass\n", "test.py", "python")
        assert len(matches) == 0


class TestCompareRightConstraint:
    """Covers _check_constraint 'right' → _check_node_spec (line 157).

    Note: ast.Compare has .comparators not .right, so 'right' on Compare
    returns None (getattr fallback) → covers the None branch at line 184.
    Use BinOp which has .left/.right to exercise the actual check.
    """

    def test_match_right_name_on_binop(self):
        pat = ASTPattern(
            id="test-right-name-binop",
            language="python",
            node_type="BinOp",
            constraints={"right": {"type": "Constant"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_no_match_right_wrong_type(self):
        pat = ASTPattern(
            id="test-right-wrong",
            language="python",
            node_type="BinOp",
            constraints={"right": {"type": "Name"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0

    def test_right_none_on_compare(self):
        """Compare has no .right → getattr returns None → _check_node_spec returns False."""
        pat = ASTPattern(
            id="test-right-cmp-none",
            language="python",
            node_type="Compare",
            constraints={"right": {"type": "Constant"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 10:\n    pass\n", "test.py", "python")
        assert len(matches) == 0


class TestFuncConstraint:
    """Covers _check_constraint 'func' → _check_node_spec (line 159)."""

    def test_match_func_attribute(self):
        pat = ASTPattern(
            id="test-func-attr",
            language="python",
            node_type="Call",
            constraints={"func": {"type": "Attribute", "attr": "lower"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = value.lower()\n", "test.py", "python")
        assert len(matches) == 1

    def test_no_match_wrong_attr(self):
        pat = ASTPattern(
            id="test-func-wrong",
            language="python",
            node_type="Call",
            constraints={"func": {"type": "Attribute", "attr": "upper"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = value.lower()\n", "test.py", "python")
        assert len(matches) == 0

    def test_match_func_name(self):
        pat = ASTPattern(
            id="test-func-name",
            language="python",
            node_type="Call",
            constraints={"func": {"type": "Name", "id": "foo"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 1


class TestOpsConstraint:
    """Covers _check_constraint 'ops' → _check_ops (lines 166-174)."""

    def test_match_ops_string_list(self):
        pat = ASTPattern(
            id="test-ops-str",
            language="python",
            node_type="Compare",
            constraints={"ops": ["Gt"]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 5:\n    pass\n", "test.py", "python")
        assert len(matches) == 1

    def test_match_ops_dict_spec(self):
        pat = ASTPattern(
            id="test-ops-dict",
            language="python",
            node_type="Compare",
            constraints={"ops": [{"type": "Gt"}]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 5:\n    pass\n", "test.py", "python")
        assert len(matches) == 1

    def test_no_match_ops_wrong_op(self):
        pat = ASTPattern(
            id="test-ops-wrong",
            language="python",
            node_type="Compare",
            constraints={"ops": ["Lt"]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 5:\n    pass\n", "test.py", "python")
        assert len(matches) == 0

    def test_no_match_ops_wrong_length(self):
        pat = ASTPattern(
            id="test-ops-len",
            language="python",
            node_type="Compare",
            constraints={"ops": ["Gt", "Lt"]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 5:\n    pass\n", "test.py", "python")
        assert len(matches) == 0


class TestTestConstraint:
    """Covers _check_constraint 'test' → _check_node_spec (line 167)."""

    def test_match_test_compare(self):
        pat = ASTPattern(
            id="test-test-cmp",
            language="python",
            node_type="If",
            constraints={"test": {"type": "Compare"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 5:\n    pass\n", "test.py", "python")
        assert len(matches) == 1


class TestTypeFieldConstraint:
    """Covers _check_constraint 'type' → _check_type_field (lines 169, 342-344).

    _check_type_field does: getattr(node, 'type', None) == value (string compare).
    ast.ExceptHandler.type is an ast.Name node, so string compare fails.
    Use an annotation context where .type is a string, or accept the mismatch
    for coverage of the string path (line 343). Also covers the non-string path.
    """

    def test_type_field_string_match(self):
        """Match when node.type is a string equal to the value."""
        # ast.keyword has 'arg' not 'type', and ExceptHandler.type is ast.Name.
        # We test the positive path by checking what actually returns True.
        # For ExceptHandler, .type is an ast.Name node, so this returns False.
        pat = ASTPattern(
            id="test-type-field",
            language="python",
            node_type="ExceptHandler",
            constraints={"type": "Exception"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "try:\n    pass\nexcept Exception:\n    pass\n", "test.py", "python"
        )
        # .type is ast.Name(id='Exception'), not the string "Exception"
        assert len(matches) == 0

    def test_type_field_non_string_returns_false(self):
        """Non-string value spec returns False (line 344)."""
        pat = ASTPattern(
            id="test-type-nonstr",
            language="python",
            node_type="ExceptHandler",
            constraints={"type": 42},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "try:\n    pass\nexcept Exception:\n    pass\n", "test.py", "python"
        )
        assert len(matches) == 0


class TestArgsConstraint:
    """Covers _check_constraint 'args' → _check_args (lines 170, 347-356)."""

    def test_match_args_constant(self):
        pat = ASTPattern(
            id="test-args-const",
            language="python",
            node_type="Call",
            constraints={"args": [{"type": "Constant"}]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo(42)\n", "test.py", "python")
        assert len(matches) == 1

    def test_no_match_args_wrong(self):
        pat = ASTPattern(
            id="test-args-name",
            language="python",
            node_type="Call",
            constraints={"args": [{"type": "Name"}]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo(42)\n", "test.py", "python")
        assert len(matches) == 0

    def test_no_match_args_too_many(self):
        pat = ASTPattern(
            id="test-args-many",
            language="python",
            node_type="Call",
            constraints={"args": [{"type": "Constant"}, {"type": "Constant"}]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo(42)\n", "test.py", "python")
        assert len(matches) == 0

    def test_match_args_with_none_slot(self):
        """None in spec means 'any' at that position."""
        pat = ASTPattern(
            id="test-args-none",
            language="python",
            node_type="Call",
            constraints={"args": [None, {"type": "Name"}]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo(42, bar)\n", "test.py", "python")
        assert len(matches) == 1


class TestBodyContainsConstraint:
    """Covers _check_constraint 'body_contains' → _check_body_contains (lines 172-174, 365-377)."""

    def test_match_body_contains_return(self):
        pat = ASTPattern(
            id="test-body-ret",
            language="python",
            node_type="FunctionDef",
            constraints={"body_contains": {"type": "Return"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return 1\n", "test.py", "python"
        )
        assert len(matches) == 1

    def test_no_match_body_missing(self):
        pat = ASTPattern(
            id="test-body-miss",
            language="python",
            node_type="FunctionDef",
            constraints={"body_contains": {"type": "Raise"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return 1\n", "test.py", "python"
        )
        assert len(matches) == 0


# ── Constraint Coverage: negation, unknown node_type, _check_op no-op ──


class TestNegationConstraints:
    """Covers negation_constraints branch (lines 136-139)."""

    def test_negation_excludes_matching(self):
        pat = ASTPattern(
            id="test-neg-excl",
            language="python",
            node_type="Call",
            constraints={},
            negation_constraints={"func": {"type": "Name", "id": "len"}},
        )
        matcher = ASTPatternMatcher([pat])
        # len(x) has func=Name(id='len') → negated out; bar(x) should match
        matches = matcher.find_matches("y = len(x)\nz = bar(x)\n", "test.py", "python")
        assert len(matches) == 1
        assert matches[0].line == 2

    def test_negation_all_excluded(self):
        pat = ASTPattern(
            id="test-neg-all",
            language="python",
            node_type="Call",
            constraints={},
            negation_constraints={"func": {"type": "Name", "id": "len"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("y = len(x)\n", "test.py", "python")
        assert len(matches) == 0


class TestUnknownNodeType:
    """Covers the getattr-None branch (line 128)."""

    def test_unknown_node_type_returns_empty(self):
        pat = ASTPattern(
            id="test-unknown-node",
            language="python",
            node_type="FakeNodeXYZ",
            constraints={},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


class TestCheckOpNoOp:
    """Covers _check_op when node has no 'op' attribute (line 179)."""

    def test_op_constraint_on_non_binop(self):
        pat = ASTPattern(
            id="test-op-noop",
            language="python",
            node_type="Name",
            constraints={"op": "Add"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1\n", "test.py", "python")
        assert len(matches) == 0


# ── Constraint Coverage: _check_node_spec dict spec deeper cases ──


class TestNodeSpecDictDeep:
    """Covers _check_node_spec dict branch with attr, id, value (lines 185-207)."""

    def test_node_spec_value_nested(self):
        """Covers the 'value' recursion in _check_node_spec (lines 203-205)."""
        pat = ASTPattern(
            id="test-spec-val",
            language="python",
            node_type="Assign",
            constraints={},
        )
        # Test via the 'left' path indirectly — use Call with func spec
        pat2 = ASTPattern(
            id="test-spec-val2",
            language="python",
            node_type="Call",
            constraints={
                "func": {
                    "type": "Attribute",
                    "attr": "lower",
                    "value": {"type": "Name", "id": "x"},
                }
            },
        )
        matcher = ASTPatternMatcher([pat2])
        matches = matcher.find_matches("x.lower()\n", "test.py", "python")
        assert len(matches) == 1

    def test_node_spec_value_mismatch(self):
        pat = ASTPattern(
            id="test-spec-val-miss",
            language="python",
            node_type="Call",
            constraints={
                "func": {
                    "type": "Attribute",
                    "attr": "lower",
                    "value": {"type": "Name", "id": "y"},
                }
            },
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x.lower()\n", "test.py", "python")
        assert len(matches) == 0

    def test_node_spec_fallback_false(self):
        """Non-string, non-dict spec returns False (line 207)."""
        pat = ASTPattern(
            id="test-spec-fallback",
            language="python",
            node_type="Call",
            constraints={"func": 42},  # int spec → fallback False
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 0


# ── Constraint Coverage: _check_parent with dict spec ──


class TestParentDictSpec:
    """Covers _check_parent dict branch (lines 218-231)."""

    def test_parent_dict_type(self):
        pat = ASTPattern(
            id="test-parent-dict",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"type": "Assign"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_parent_dict_wrong_type(self):
        pat = ASTPattern(
            id="test-parent-wrong",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"type": "Return"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0

    def test_parent_dict_depth(self):
        """BinOp → Assign → Module. With depth=1 (default), check parent type=Assign."""
        pat = ASTPattern(
            id="test-parent-depth",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"type": "Assign", "depth": 1}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_parent_dict_depth_walk(self):
        """Depth > 1 walks further up. BinOp → Assign → Module."""
        pat = ASTPattern(
            id="test-parent-depth2",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"type": "Assign", "depth": 2}},
        )
        matcher = ASTPatternMatcher([pat])
        # depth=2: parent is Assign, walk 1 more → Module.
        # Type check is on the initial parent (Assign), which matches.
        # Then walks to Module and returns True (Module exists).
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_parent_dict_depth_too_deep(self):
        pat = ASTPattern(
            id="test-parent-toodeep",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"type": "Module", "depth": 5}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0

    def test_parent_string_spec(self):
        """String spec for parent (line 216-217)."""
        pat = ASTPattern(
            id="test-parent-str",
            language="python",
            node_type="BinOp",
            constraints={"parent": "Assign"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_parent_no_parent(self):
        """Top-level node has no parent (line 213-214)."""
        pat = ASTPattern(
            id="test-parent-none",
            language="python",
            node_type="Module",
            constraints={"parent": {"type": "Module"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1\n", "test.py", "python")
        assert len(matches) == 0

    def test_parent_fallback_invalid_spec(self):
        """Non-string, non-dict spec returns False (line 231)."""
        pat = ASTPattern(
            id="test-parent-bad",
            language="python",
            node_type="BinOp",
            constraints={"parent": 99},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


# ── Context constraint coverage ──


class TestContextVariablesInclude:
    """Covers variables_include context check (lines 248-255)."""

    def test_match_variables_include(self):
        pat = ASTPattern(
            id="test-var-inc",
            language="python",
            node_type="BinOp",
            constraints={"context": {"variables_include": ["amount"]}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "result = amount + discount\n", "test.py", "python"
        )
        assert len(matches) == 1

    def test_no_match_variables_missing(self):
        pat = ASTPattern(
            id="test-var-miss",
            language="python",
            node_type="BinOp",
            constraints={"context": {"variables_include": ["nonexistent"]}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "result = amount + discount\n", "test.py", "python"
        )
        assert len(matches) == 0


class TestContextEnclosingFunctionCallsExclude:
    """Covers enclosing_function_calls_exclude (lines 257-262)."""

    def test_match_when_excluded_call_absent(self):
        pat = ASTPattern(
            id="test-exc-calls",
            language="python",
            node_type="BinOp",
            constraints={"context": {"enclosing_function_calls_exclude": ["baz"]}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return bar() + 1\n", "test.py", "python"
        )
        assert len(matches) == 1

    def test_no_match_when_excluded_call_present(self):
        pat = ASTPattern(
            id="test-exc-calls-no",
            language="python",
            node_type="BinOp",
            constraints={"context": {"enclosing_function_calls_exclude": ["bar"]}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return bar() + 1\n", "test.py", "python"
        )
        assert len(matches) == 0


class TestContextInLoopBody:
    """Covers in_loop_body context check (lines 273-276)."""

    def test_match_in_loop(self):
        pat = ASTPattern(
            id="test-loop-body",
            language="python",
            node_type="BinOp",
            constraints={"context": {"in_loop_body": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "for x in items:\n    y = x + 1\n", "test.py", "python"
        )
        assert len(matches) == 1

    def test_no_match_not_in_loop(self):
        pat = ASTPattern(
            id="test-no-loop",
            language="python",
            node_type="BinOp",
            constraints={"context": {"in_loop_body": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("y = x + 1\n", "test.py", "python")
        assert len(matches) == 0


class TestContextInExceptHandler:
    """Covers in_except_handler context check (lines 278-280)."""

    def test_match_in_except(self):
        pat = ASTPattern(
            id="test-in-except",
            language="python",
            node_type="Call",
            constraints={"context": {"in_except_handler": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "try:\n    pass\nexcept:\n    log()\n", "test.py", "python"
        )
        assert len(matches) == 1

    def test_no_match_not_in_except(self):
        pat = ASTPattern(
            id="test-not-except",
            language="python",
            node_type="Call",
            constraints={"context": {"in_except_handler": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("log()\n", "test.py", "python")
        assert len(matches) == 0


class TestContextFunctionHasInstanceGuard:
    """Covers function_has_isinstance_guard context check (lines 282-290)."""

    def test_no_match_when_guard_present(self):
        """When value=True, has_guard=True → returns False (excluded)."""
        pat = ASTPattern(
            id="test-guard-excl",
            language="python",
            node_type="Call",
            constraints={"context": {"function_has_isinstance_guard": True}},
        )
        matcher = ASTPatternMatcher([pat])
        source = "def foo(x):\n    if isinstance(x, str):\n        return x.lower()\n"
        matches = matcher.find_matches(source, "test.py", "python")
        assert len(matches) == 0

    def test_match_when_no_guard(self):
        """When value=True, has_guard=False → does NOT return False (match)."""
        pat = ASTPattern(
            id="test-guard-ok",
            language="python",
            node_type="Call",
            constraints={"context": {"function_has_isinstance_guard": True}},
        )
        matcher = ASTPatternMatcher([pat])
        source = "def foo(x):\n    return x.lower()\n"
        matches = matcher.find_matches(source, "test.py", "python")
        assert len(matches) == 1

    def test_no_match_no_enclosing_function(self):
        """No enclosing function → returns False (line 287)."""
        pat = ASTPattern(
            id="test-guard-nofunc",
            language="python",
            node_type="Call",
            constraints={"context": {"function_has_isinstance_guard": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 0


class TestContextFunctionUsesStrMethods:
    """Covers function_uses_str_methods context check (lines 292-297)."""

    def test_match_uses_str_methods(self):
        pat = ASTPattern(
            id="test-str-methods",
            language="python",
            node_type="Call",
            constraints={"context": {"function_uses_str_methods": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo(x):\n    return x.lower()\n", "test.py", "python"
        )
        assert len(matches) == 1

    def test_no_match_no_str_methods(self):
        pat = ASTPattern(
            id="test-no-str",
            language="python",
            node_type="Call",
            constraints={"context": {"function_uses_str_methods": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo(x):\n    return x + 1\n", "test.py", "python"
        )
        assert len(matches) == 0

    def test_no_match_no_function(self):
        pat = ASTPattern(
            id="test-str-nofunc",
            language="python",
            node_type="Call",
            constraints={"context": {"function_uses_str_methods": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 0


# ── _check_ops deeper coverage ──


class TestCheckOpsNoOps:
    """Covers _check_ops when node has no 'ops' (line 327)."""

    def test_ops_on_non_compare(self):
        pat = ASTPattern(
            id="test-ops-nocmp",
            language="python",
            node_type="BinOp",
            constraints={"ops": ["Add"]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


class TestCheckArgsNoArgs:
    """Covers _check_args when node has no 'args' attr (line 348-349)."""

    def test_args_on_node_without_args(self):
        pat = ASTPattern(
            id="test-args-noattr",
            language="python",
            node_type="BinOp",
            constraints={"args": [{"type": "Constant"}]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


# ── _check_body_contains deeper coverage ──


class TestBodyContainsEdgeCases:
    """Covers _check_body_contains fallback and missing body (lines 365-377)."""

    def test_body_contains_non_dict_spec(self):
        """Non-dict spec returns False (line 377)."""
        pat = ASTPattern(
            id="test-body-nondict",
            language="python",
            node_type="FunctionDef",
            constraints={"body_contains": "Return"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return 1\n", "test.py", "python"
        )
        assert len(matches) == 0

    def test_body_contains_no_body_attr(self):
        """Node without 'body' attr returns False (line 367)."""
        pat = ASTPattern(
            id="test-body-nobody",
            language="python",
            node_type="BinOp",
            constraints={"body_contains": {"type": "Return"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


# ── find_enclosing_loop delegation (lines 394-396) ──


class TestFindEnclosingLoopDelegation:
    """Covers the find_enclosing_loop method on the matcher itself."""

    def test_delegates_to_context(self):
        matcher = ASTPatternMatcher([])
        source = "for x in items:\n    y = x + 1\n"
        tree = ast.parse(source)
        pmap = build_parent_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp):
                result = matcher.find_enclosing_loop(node, pmap)
                assert result is not None
                assert isinstance(result, ast.For)
                break


# ── _post_filter for hardcoded-tmp-path edge cases ──


class TestPostFilterHardcodedTmpPathStringEdgeCases:
    """Covers the keyword parent check (line 99) in _post_filter."""

    def test_tmp_string_in_path_call_excluded(self):
        """'/tmp/' string inside Path() call is excluded by _post_filter."""
        # The hardcoded-tmp-path-string pattern matches strings with /tmp/
        # but _post_filter filters out those inside Path() calls
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        source = "from pathlib import Path\np = Path('/tmp/test')\n"
        matches = matcher.find_matches(source, "test.py", "python")
        # The string '/tmp/test' should NOT appear as hardcoded-tmp-path-string
        string_matches = [
            m for m in matches if m.pattern.id == "hardcoded-tmp-path-string"
        ]
        assert len(string_matches) == 0

    def test_tmp_string_standalone(self):
        """Standalone '/tmp/' string IS matched by hardcoded-tmp-path-string."""
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        source = "log_path = '/tmp/app.log'\n"
        matches = matcher.find_matches(source, "test.py", "python")
        string_matches = [
            m for m in matches if m.pattern.id == "hardcoded-tmp-path-string"
        ]
        assert len(string_matches) == 1


# ── _check_constraint fallback for unknown key ──


class TestUnknownConstraintKey:
    """Covers the final 'return False' in _check_constraint (line 174)."""

    def test_unknown_key_returns_false(self):
        pat = ASTPattern(
            id="test-unknown-key",
            language="python",
            node_type="BinOp",
            constraints={"nonexistent_constraint_key": "whatever"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


# ── Combined constraint patterns ──


class TestCombinedConstraints:
    """Tests patterns combining multiple constraint types together."""

    def test_left_op_on_binop(self):
        pat = ASTPattern(
            id="test-combined",
            language="python",
            node_type="BinOp",
            constraints={
                "left": {"type": "Name", "id": "x"},
                "op": "Add",
                "right": {"type": "Constant"},
            },
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("y = x + 5\n", "test.py", "python")
        assert len(matches) == 1

    def test_combined_mismatch(self):
        pat = ASTPattern(
            id="test-comb-miss",
            language="python",
            node_type="BinOp",
            constraints={
                "left": {"type": "Name", "id": "z"},
                "op": "Add",
                "right": {"type": "Constant"},
            },
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("y = x + 5\n", "test.py", "python")
        assert len(matches) == 0

    def test_func_and_args_combined(self):
        pat = ASTPattern(
            id="test-func-args",
            language="python",
            node_type="Call",
            constraints={
                "func": {"type": "Name", "id": "foo"},
                "args": [{"type": "Constant"}],
            },
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo(42)\nbar(x)\n", "test.py", "python")
        assert len(matches) == 1
        assert matches[0].line == 1


# ── _check_node_spec string spec (line 186-188) ──


class TestNodeSpecString:
    """Covers _check_node_spec with string type spec (line 186-188)."""

    def test_string_spec_match(self):
        pat = ASTPattern(
            id="test-str-spec",
            language="python",
            node_type="Call",
            constraints={"func": "Name"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 1

    def test_string_spec_no_match(self):
        pat = ASTPattern(
            id="test-str-nomatch",
            language="python",
            node_type="Call",
            constraints={"func": "Attribute"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 0

    def test_string_spec_invalid_type(self):
        """Invalid AST type name → getattr returns None → False."""
        pat = ASTPattern(
            id="test-str-bad",
            language="python",
            node_type="Call",
            constraints={"func": "NonexistentType"},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 0


# ── Remaining edge cases for lines 73, 99, 229, 244 ──


class TestPostFilterPerfPopFrontNonCall:
    """Covers line 73: _post_filter perf-pop-front-loop on a non-Call node.

    The real perf-pop-front-loop pattern has node_type="Call" so _post_filter
    always sees Call nodes. We create a custom pattern with the same id but a
    different node_type to force the non-Call branch.
    """

    def test_post_filter_non_call_returns_false(self):
        # Create a pattern with the perf-pop-front-loop id but node_type=Name
        # so _post_filter hits `not isinstance(node, ast.Call)` → False
        pat = ASTPattern(
            id="perf-pop-front-loop",
            language="python",
            node_type="Name",
            constraints={},
        )
        matcher = ASTPatternMatcher([pat])
        # Name 'x' will match node_type=Name and pass _matches_pattern,
        # but _post_filter will return False because it's not a Call.
        matches = matcher.find_matches("x = 1\n", "test.py", "python")
        assert len(matches) == 0


class TestPostFilterTmpStringKeywordArg:
    """Covers line 99: _post_filter hardcoded-tmp-path-string when parent is keyword.

    Uses the real patterns from PYTHON_PATTERNS.
    """

    def test_tmp_string_as_keyword_arg_excluded(self):
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        # open(file='/tmp/test.txt') — the '/tmp/test.txt' string is a keyword arg value
        source = "open(file='/tmp/test.txt')\n"
        matches = matcher.find_matches(source, "test.py", "python")
        string_matches = [
            m for m in matches if m.pattern.id == "hardcoded-tmp-path-string"
        ]
        assert len(string_matches) == 0


class TestParentDepthWalkPastRoot:
    """Covers line 229: _check_parent depth walk hits None.

    Assign is top-level → parent is Module → depth=2 walks past Module → None.
    """

    def test_parent_depth_walks_past_root(self):
        # Assign → Module (depth 1). depth=3 walks 2 more steps: Module → None.
        pat = ASTPattern(
            id="test-parent-past-root",
            language="python",
            node_type="Assign",
            constraints={"parent": {"type": "Module", "depth": 3}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1\n", "test.py", "python")
        assert len(matches) == 0


class TestContextFunctionNamePatternNoFunction:
    """Covers line 244: function_name_pattern when no enclosing function."""

    def test_no_enclosing_function(self):
        pat = ASTPattern(
            id="test-fname-nofunc",
            language="python",
            node_type="BinOp",
            constraints={"context": {"function_name_pattern": "foo"}},
        )
        matcher = ASTPatternMatcher([pat])
        # BinOp at module level → no enclosing function → returns False
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0
