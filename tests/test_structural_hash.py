import pytest
from pathlib import Path

import ast

from bluei.engine.structural_hash import (
    compute_structural_hash,
    normalize_for_sharing,
    extract_node_sequence,
    extract_operator_sequence,
    fuzzy_structural_match,
    sequence_similarity,
    get_fuzzy_threshold,
    _levenshtein_distance,
    _SharingNormalizer,
    FUZZY_THRESHOLDS,
    DEFAULT_FUZZY_THRESHOLD,
)
from bluei.engine.pattern_store import (
    FixPattern,
    FixPatternStore,
    normalize_snippet,
)


class TestPythonStructuralHash:
    def test_invariant_to_variable_names(self):
        h1 = compute_structural_hash("result = amount + discount", "python")
        h2 = compute_structural_hash("total = price + rebate", "python")
        assert h1 == h2

    def test_invariant_to_variable_names_complex(self):
        h1 = compute_structural_hash("x = foo(a, b)", "python")
        h2 = compute_structural_hash("y = bar(c, d)", "python")
        assert h1 == h2

    def test_invariant_to_string_literals(self):
        h1 = compute_structural_hash('name = "alice"', "python")
        h2 = compute_structural_hash('name = "bob"', "python")
        assert h1 == h2

    def test_invariant_to_number_literals(self):
        h1 = compute_structural_hash("count = 42", "python")
        h2 = compute_structural_hash("count = 99", "python")
        assert h1 == h2

    def test_invariant_to_whitespace(self):
        h1 = compute_structural_hash("x = 1\ny = 2", "python")
        h2 = compute_structural_hash("x  =  1\ny  =  2", "python")
        assert h1 == h2

    def test_sensitive_to_operators(self):
        h1 = compute_structural_hash("result = amount + discount", "python")
        h2 = compute_structural_hash("result = amount - discount", "python")
        assert h1 != h2

    def test_sensitive_to_structure(self):
        h1 = compute_structural_hash("if x:\n    y()", "python")
        h2 = compute_structural_hash("for x in []:\n    z()", "python")
        assert h1 != h2

    def test_same_code_same_hash(self):
        code = "result = a + b"
        h1 = compute_structural_hash(code, "python")
        h2 = compute_structural_hash(code, "python")
        assert h1 == h2

    def test_comparison_operators_distinguished(self):
        h1 = compute_structural_hash("if x > 0:", "python")
        h2 = compute_structural_hash("if x < 0:", "python")
        assert h1 != h2

    def test_bool_ops_distinguished(self):
        h1 = compute_structural_hash("x = True and False", "python")
        h2 = compute_structural_hash("x = True or False", "python")
        assert h1 != h2

    def test_augmented_assignment(self):
        h1 = compute_structural_hash("x += 1", "python")
        h2 = compute_structural_hash("x -= 1", "python")
        assert h1 != h2

    def test_multiline(self):
        h1 = compute_structural_hash("def foo(a, b):\n    return a + b", "python")
        h2 = compute_structural_hash("def bar(x, y):\n    return x + y", "python")
        assert h1 == h2

    def test_invalid_syntax_falls_back(self):
        h = compute_structural_hash("}}}{invalid{{{", "python")
        assert h is not None
        assert len(h) == 16

    def test_empty_code(self):
        h = compute_structural_hash("", "python")
        assert h is not None


class TestTextStructuralHash:
    def test_javascript_uses_text_hash(self):
        h1 = compute_structural_hash("let x = 1;", "javascript")
        h2 = compute_structural_hash("let x = 1;", "javascript")
        assert h1 == h2

    def test_go_uses_text_hash(self):
        h1 = compute_structural_hash("func main() {}", "go")
        assert h1 is not None

    def test_text_hash_strips_comments(self):
        h1 = compute_structural_hash("x = 1  # comment", "javascript")
        h2 = compute_structural_hash("x = 1  # different", "javascript")
        assert h1 == h2

    def test_text_hash_normalizes_identifiers(self):
        h1 = compute_structural_hash("foo = bar + baz", "javascript")
        h2 = compute_structural_hash("one = two + three", "javascript")
        assert h1 == h2

    def test_text_hash_preserves_operators(self):
        h1 = compute_structural_hash("x = a + b", "javascript")
        h2 = compute_structural_hash("x = a - b", "javascript")
        assert h1 != h2


class TestNodeSequence:
    def test_python_node_sequence(self):
        seq = extract_node_sequence("x = 1 + 2", "python")
        assert "Module" in seq
        assert "Assign" in seq
        assert "BinOp" in seq

    def test_text_node_sequence(self):
        seq = extract_node_sequence("x = a + b", "javascript")
        assert "ID" in seq
        assert "+" in seq
        assert "=" in seq

    def test_empty_code_node_sequence(self):
        seq = extract_node_sequence("", "python")
        assert isinstance(seq, list)

    def test_node_sequence_length_varies_with_complexity(self):
        simple = extract_node_sequence("x = 1", "python")
        complex_code = extract_node_sequence("if x:\n    y = a + b\n    z()", "python")
        assert len(complex_code) > len(simple)


class TestOperatorSequence:
    def test_python_operator_sequence(self):
        ops = extract_operator_sequence("x = a + b * c", "python")
        assert "Mult" in ops
        assert "Add" in ops

    def test_python_comparison_operators(self):
        ops = extract_operator_sequence("x = a > b and c < d", "python")
        assert "Gt" in ops
        assert "Lt" in ops
        assert "And" in ops

    def test_text_operator_sequence(self):
        ops = extract_operator_sequence("x = a + b - c", "javascript")
        assert "+" in ops
        assert "-" in ops

    def test_no_operators_empty(self):
        ops = extract_operator_sequence("x", "python")
        assert ops == []


class TestSequenceSimilarity:
    def test_identical_sequences(self):
        assert sequence_similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0

    def test_completely_different(self):
        assert sequence_similarity(["a", "b"], ["x", "y"]) == 0.0

    def test_empty_both(self):
        assert sequence_similarity([], []) == 1.0

    def test_empty_one(self):
        assert sequence_similarity(["a"], []) == 0.0

    def test_partial_overlap(self):
        score = sequence_similarity(["a", "b", "c"], ["a", "x", "c"])
        assert 0.0 < score < 1.0

    def test_insertion(self):
        score = sequence_similarity(["a", "b"], ["a", "x", "b"])
        assert score > 0.5


class TestFuzzyStructuralMatch:
    def test_exact_match_returns_1(self):
        score = fuzzy_structural_match(
            "result = amount + discount",
            "result = amount + discount",
            "python",
        )
        assert score == 1.0

    def test_structural_equivalent_high_score(self):
        score = fuzzy_structural_match(
            "result = amount + discount",
            "total = price + rebate",
            "python",
        )
        assert score == 1.0

    def test_different_operators_low_score(self):
        score = fuzzy_structural_match(
            "result = amount + discount",
            "result = amount - discount",
            "python",
        )
        assert score < 1.0

    def test_completely_different(self):
        score = fuzzy_structural_match(
            "for item in items:\n    process(item)",
            "class MyObject:\n    pass",
            "python",
        )
        assert score < 0.5

    def test_similar_structure_different_vars(self):
        score = fuzzy_structural_match(
            "if user_name in active_users:",
            "if username in active:",
            "python",
        )
        assert score >= 0.5

    def test_no_operators_uses_node_similarity(self):
        score = fuzzy_structural_match(
            "foo()",
            "bar()",
            "python",
        )
        assert score >= 0.5


class TestFuzzyThresholds:
    def test_bug_category(self):
        catalog = [{"rule": "test-rule", "category": "bug"}]
        assert get_fuzzy_threshold("test-rule", catalog) == 0.85

    def test_lint_category(self):
        catalog = [{"rule": "test-rule", "category": "lint"}]
        assert get_fuzzy_threshold("test-rule", catalog) == 0.70

    def test_unknown_rule_uses_default(self):
        catalog = [{"rule": "other-rule", "category": "bug"}]
        assert get_fuzzy_threshold("test-rule", catalog) == DEFAULT_FUZZY_THRESHOLD

    def test_no_catalog_uses_default(self):
        assert get_fuzzy_threshold("any-rule") == DEFAULT_FUZZY_THRESHOLD

    def test_perf_smell_category(self):
        catalog = [{"rule": "test-rule", "category": "perf-smell"}]
        assert get_fuzzy_threshold("test-rule", catalog) == 0.80


class TestNormalizeForSharing:
    def test_python_strips_variable_names(self):
        normalized = normalize_for_sharing("from mycorp.secrets import KEY", "python")
        assert "mycorp" not in normalized
        assert "KEY" not in normalized

    def test_python_replaces_strings(self):
        normalized = normalize_for_sharing('password = "supersecret"', "python")
        assert "supersecret" not in normalized

    def test_python_replaces_numbers(self):
        normalized = normalize_for_sharing("count = 42", "python")
        assert normalized is not None

    def test_text_normalization(self):
        normalized = normalize_for_sharing("x = secret_value", "javascript")
        assert "secret_value" not in normalized

    def test_invalid_syntax_fallback(self):
        normalized = normalize_for_sharing("}}}{invalid{{{", "python")
        assert isinstance(normalized, str)


class TestFixPatternStoreStructural:
    def test_append_sets_structural_hash(self, tmp_path):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pattern = FixPattern(
            pattern_id="",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            diff_patch="fake",
        )
        pid = store.append(pattern)
        stored = store.get_pattern(pid)
        assert stored is not None
        assert stored.structural_hash is not None
        assert len(stored.structural_hash) == 16

    def test_lookup_structural_different_vars(self, tmp_path):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pattern = FixPattern(
            pattern_id="",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            diff_patch="fake",
            confidence=0.9,
        )
        store.append(pattern)

        found = store.lookup_structural("test-rule", "total = price + rebate", "python")
        assert found is not None
        assert found.rule == "test-rule"

    def test_lookup_structural_miss(self, tmp_path):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pattern = FixPattern(
            pattern_id="",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            diff_patch="fake",
        )
        store.append(pattern)

        found = store.lookup_structural("test-rule", "x = list.pop(0)", "python")
        assert found is None

    def test_lookup_fuzzy_similar_patterns(self, tmp_path):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pattern = FixPattern(
            pattern_id="",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            diff_patch="fake",
            confidence=0.9,
        )
        store.append(pattern)

        from bluei.engine.constants import DETECTOR_CATALOG

        found = store.lookup_fuzzy(
            "test-rule",
            "total = price + rebate",
            "python",
            catalog=DETECTOR_CATALOG,
        )
        assert found is not None

    def test_lookup_fuzzy_no_match(self, tmp_path):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pattern = FixPattern(
            pattern_id="",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="result = amount + discount",
            after_snippet="result = amount - discount",
            diff_patch="fake",
        )
        store.append(pattern)

        found = store.lookup_fuzzy(
            "test-rule",
            "def completely_different():\n    pass",
            "python",
        )
        assert found is None

    def test_backward_compat_no_structural_hash(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        import json

        store_path.write_text(
            json.dumps(
                {
                    "pattern_id": "fp-old",
                    "rule": "old-rule",
                    "language": "python",
                    "file_path": "test.py",
                    "before_snippet": "x = 1",
                    "after_snippet": "x = 2",
                    "diff_patch": "fake",
                    "confidence": 0.8,
                }
            )
            + "\n"
        )
        store = FixPatternStore(store_path)
        pattern = store.get_pattern("fp-old")
        assert pattern is not None
        assert pattern.structural_hash is None

        found = store.lookup("old-rule", "x = 1")
        assert found is not None

    def test_structural_hash_computed_on_rebuild(self, tmp_path):
        store_path = tmp_path / "patterns.jsonl"
        import json

        store_path.write_text(
            json.dumps(
                {
                    "pattern_id": "fp-old",
                    "rule": "old-rule",
                    "language": "python",
                    "file_path": "test.py",
                    "before_snippet": "x = 1",
                    "after_snippet": "x = 2",
                    "diff_patch": "fake",
                    "confidence": 0.8,
                }
            )
            + "\n"
        )
        store = FixPatternStore(store_path)
        found = store.lookup_structural("old-rule", "y = 2", "python")
        assert found is not None

    def test_to_dict_includes_structural_hash(self, tmp_path):
        store = FixPatternStore(tmp_path / "patterns.jsonl")
        pattern = FixPattern(
            pattern_id="fp-test",
            rule="test-rule",
            language="python",
            file_path="test.py",
            before_snippet="x = 1",
            after_snippet="x = 2",
            diff_patch="fake",
            structural_hash="abcd1234",
        )
        d = pattern.to_dict()
        assert d["structural_hash"] == "abcd1234"

    def test_from_dict_with_structural_hash(self):
        d = {
            "pattern_id": "fp-test",
            "rule": "test-rule",
            "language": "python",
            "file_path": "test.py",
            "before_snippet": "x = 1",
            "after_snippet": "x = 2",
            "diff_patch": "fake",
            "structural_hash": "abcd1234",
        }
        pattern = FixPattern.from_dict(d)
        assert pattern.structural_hash == "abcd1234"

    def test_from_dict_without_structural_hash(self):
        d = {
            "pattern_id": "fp-test",
            "rule": "test-rule",
            "language": "python",
            "file_path": "test.py",
            "before_snippet": "x = 1",
            "after_snippet": "x = 2",
            "diff_patch": "fake",
        }
        pattern = FixPattern.from_dict(d)
        assert pattern.structural_hash is None


# ── Extracted from test_medium_modules_remaining.py ──
# Additional AST node types, operator sequences, edge cases


class TestStructuralHashCallKwargs:
    def test_call_with_args_and_kwargs(self):
        h = compute_structural_hash("foo(1, key=2)", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_call_with_multiple_kwargs(self):
        h = compute_structural_hash("foo(a=1, b=2)", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashNoneConstant:
    def test_none_constant(self):
        h = compute_structural_hash("x = None", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashListElements:
    def test_list_two_elements(self):
        h = compute_structural_hash("x = [1, 2]", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_list_three_elements(self):
        h = compute_structural_hash("x = [1, 2, 3]", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashDictWithKeys:
    def test_dict_string_key(self):
        h = compute_structural_hash("d = {'a': 1}", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_dict_multiple_keys(self):
        h = compute_structural_hash("d = {'a': 1, 'b': 2}", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashSlice:
    def test_slice_full(self):
        h = compute_structural_hash("x[1:10:2]", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_slice_no_step(self):
        h = compute_structural_hash("x[1:10]", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_slice_lower_only(self):
        h = compute_structural_hash("x[1:]", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_slice_upper_only(self):
        h = compute_structural_hash("x[:10]", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashIfElse:
    def test_if_else(self):
        h = compute_structural_hash("if a:\n    b\nelse:\n    c", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashWhile:
    def test_while_loop(self):
        h = compute_structural_hash("while a:\n    b", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashAsyncMultiArg:
    def test_async_two_args(self):
        h = compute_structural_hash("async def f(a, b):\n    pass", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashBreakContinue:
    def test_break(self):
        h = compute_structural_hash("while True:\n    break", "python")
        assert isinstance(h, str) and len(h) == 16

    def test_continue(self):
        h = compute_structural_hash("while True:\n    continue", "python")
        assert isinstance(h, str) and len(h) == 16


class TestStructuralHashStarred:
    def test_starred_in_list(self):
        h = compute_structural_hash("x = [*a]", "python")
        assert isinstance(h, str) and len(h) == 16


class TestNodeSequenceSyntaxError:
    def test_syntax_error_fallback(self):
        result = extract_node_sequence("}}}{invalid{{{", "python")
        assert isinstance(result, list)


class TestOperatorSequenceSyntaxError:
    def test_syntax_error_fallback(self):
        result = extract_operator_sequence("}}}{invalid{{{", "python")
        assert isinstance(result, list)


class TestOperatorExtractorUnaryOp:
    def test_not_operator(self):
        ops = extract_operator_sequence("not x", "python")
        assert "Not" in ops

    def test_negate_operator(self):
        ops = extract_operator_sequence("-x", "python")
        assert "USub" in ops


class TestOperatorExtractorAugAssign:
    def test_aug_assign_add(self):
        ops = extract_operator_sequence("x += 1", "python")
        assert "Add" in ops

    def test_aug_assign_sub(self):
        ops = extract_operator_sequence("x -= 1", "python")
        assert "Sub" in ops


class TestLevenshteinEdgeCases:
    def test_empty_a(self):
        assert _levenshtein_distance([], ["a", "b"]) == 2

    def test_empty_b(self):
        assert _levenshtein_distance(["a", "b"], []) == 2

    def test_both_empty(self):
        assert _levenshtein_distance([], []) == 0


class TestSharingNormalizerVarCache:
    def test_same_var_twice_hits_cache(self):
        result = normalize_for_sharing("x = x + 1", "python")
        assert isinstance(result, str)

    def test_import_normalized(self):
        result = normalize_for_sharing("import os", "python")
        assert "_mod" in result

    def test_import_from_normalized(self):
        result = normalize_for_sharing("from os import path", "python")
        assert "_mod" in result


class TestStructuralHashDeadCodeDirect:
    def test_visit_index_directly(self):
        from bluei.engine.structural_hash import _PythonStructuralNormalizer

        norm = _PythonStructuralNormalizer()

        class _FakeIndex:
            value = ast.Constant(value=1)

        norm.visit_Index(_FakeIndex())
        assert "<num>" in norm.tokens

    def test_visit_formatted_value_directly(self):
        from bluei.engine.structural_hash import _PythonStructuralNormalizer

        norm = _PythonStructuralNormalizer()
        fv_node = ast.FormattedValue(
            value=ast.Name(id="x", ctx=ast.Load()),
            conversion=-1,
            format_spec=None,
        )
        norm.visit_FormattedValue(fv_node)
        assert len(norm.tokens) > 0
