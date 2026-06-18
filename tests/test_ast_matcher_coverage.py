"""Branch-coverage tests for ``bluei.engine.ast_engine.matcher``.

These tests target the partial branches reported by ``--cov-branch`` that the
existing statement-level tests in ``test_ast_detection.py`` do not reach.
Each test parses a small, real Python source snippet — no AST mocking — and
asserts on the number of matches returned by :class:`ASTPatternMatcher`.
"""

from __future__ import annotations

from bluei.engine.ast_engine.matcher import ASTPatternMatcher
from bluei.engine.ast_engine.models import ASTPattern


# ── _post_filter: hardcoded-tmp-path loop fallback (branch 88->87) ──


class TestPostFilterHardcodedTmpPathLoopFallback:
    """Covers the loop-back inside ``hardcoded-tmp-path`` post_filter.

    Branch ``88->87`` fires when a ``Path(...)`` call has an argument that is
    NOT a string ``Constant`` (e.g. a ``Name``), so the inner ``isinstance``
    check is False and iteration continues to the next argument.
    """

    def test_non_string_arg_before_tmp_string_still_matches(self):
        """A ``Name`` arg followed by a ``/tmp/`` string exercises the loop-back.

        The matcher still returns the call because the second arg carries
        ``/tmp/``; reaching it requires iterating past the non-string ``Name``.
        """
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        source = "p = Path(base, '/tmp/x')\n"
        matches = matcher.find_matches(source, "test.py", "python")
        tmp_matches = [m for m in matches if m.pattern.id == "hardcoded-tmp-path"]
        assert len(tmp_matches) == 1

    def test_only_non_string_args_yields_no_match(self):
        """When no argument is a ``/tmp/`` string, the loop exhausts → False."""
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        source = "p = Path(base, other)\n"
        matches = matcher.find_matches(source, "test.py", "python")
        tmp_matches = [m for m in matches if m.pattern.id == "hardcoded-tmp-path"]
        assert len(tmp_matches) == 0


# ── _post_filter: hardcoded-tmp-path-string inside non-Path call (101->103) ──


class TestPostFilterTmpPathStringInNonPathCall:
    """Covers branch ``101->103`` in ``hardcoded-tmp-path-string``.

    The filter excludes ``/tmp/`` strings whose parent is a ``Path(...)`` call,
    but a ``/tmp/`` string inside any *other* call (e.g. ``open``) survives.
    """

    def test_tmp_string_inside_open_call_matches(self):
        """``open('/tmp/x')`` → parent is a Call whose func is not ``Path``.

        Line 101 (``func.id == "Path"``) is False, so we fall through to the
        ``return True`` on line 103.
        """
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        source = "f = open('/tmp/x')\n"
        matches = matcher.find_matches(source, "test.py", "python")
        string_matches = [
            m for m in matches if m.pattern.id == "hardcoded-tmp-path-string"
        ]
        assert len(string_matches) == 1

    def test_tmp_string_inside_attribute_call_matches(self):
        """Parent call with an ``Attribute`` func (not a ``Name``) also survives."""
        from bluei.engine.ast_engine.patterns.python_patterns import PYTHON_PATTERNS

        matcher = ASTPatternMatcher(PYTHON_PATTERNS)
        source = "os.makedirs('/tmp/inner', exist_ok=True)\n"
        matches = matcher.find_matches(source, "test.py", "python")
        string_matches = [
            m for m in matches if m.pattern.id == "hardcoded-tmp-path-string"
        ]
        assert len(string_matches) == 1


# ── _check_node_spec: dict spec without 'type' (191->195) ──


class TestCheckNodeSpecDictWithoutType:
    """Covers branch ``191->195``.

    A dict spec lacking a ``type`` key skips the type check and proceeds to the
    ``attr``/``id`` checks.
    """

    def test_dict_spec_with_only_attr_matches(self):
        pat = ASTPattern(
            id="test-nodespec-noattr-type",
            language="python",
            node_type="Call",
            constraints={"func": {"attr": "lower"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = value.lower()\n", "test.py", "python")
        assert len(matches) == 1

    def test_dict_spec_with_only_id_matches(self):
        pat = ASTPattern(
            id="test-nodespec-id-only",
            language="python",
            node_type="Call",
            constraints={"func": {"id": "foo"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("foo()\n", "test.py", "python")
        assert len(matches) == 1

    def test_dict_spec_attr_mismatch_no_match(self):
        pat = ASTPattern(
            id="test-nodespec-attr-miss",
            language="python",
            node_type="Call",
            constraints={"func": {"attr": "upper"}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = value.lower()\n", "test.py", "python")
        assert len(matches) == 0


# ── _check_parent: dict spec without 'type' (220->224) ──


class TestCheckParentDictWithoutType:
    """Covers branch ``220->224``.

    A parent dict spec with only a ``depth`` (no ``type``) skips the type check
    and walks up by ``depth``.
    """

    def test_parent_depth_without_type_matches(self):
        pat = ASTPattern(
            id="test-parent-no-type",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"depth": 1}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_parent_depth_without_type_too_deep_no_match(self):
        pat = ASTPattern(
            id="test-parent-no-type-deep",
            language="python",
            node_type="BinOp",
            constraints={"parent": {"depth": 5}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


# ── _check_context: enclosing_function_calls_exclude with no function (259->240) ──


class TestContextEnclosingFunctionCallsExcludeNoFunction:
    """Covers branch ``259->240``.

    When the matched node sits at module level there is no enclosing function,
    so ``find_enclosing_function`` returns ``None`` and the ``if func_def:``
    check is False → loop continues (back to line 240).
    """

    def test_module_level_node_passes_exclude_check(self):
        pat = ASTPattern(
            id="test-exc-calls-nofunc",
            language="python",
            node_type="BinOp",
            constraints={"context": {"enclosing_function_calls_exclude": ["baz"]}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1


# ── _check_context: function_uses_str_methods paths ──


class TestContextFunctionUsesStrMethods:
    """Targets branch ``292->240`` and the surrounding str-methods logic."""

    def test_unknown_context_key_skips_all_elifs(self):
        """An unrecognized key falls through every elif (incl. line 292) → loop.

        This drives the last elif (``function_uses_str_methods``) False, taking
        the ``292->240`` arc, and the constraint overall passes.
        """
        pat = ASTPattern(
            id="test-unknown-ctx-key",
            language="python",
            node_type="BinOp",
            constraints={"context": {"completely_unknown_key": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 1

    def test_str_methods_present_in_enclosing_function_matches(self):
        """Node inside a function that uses a str method → check passes."""
        pat = ASTPattern(
            id="test-strmethods-present",
            language="python",
            node_type="BinOp",
            constraints={"context": {"function_uses_str_methods": True}},
        )
        matcher = ASTPatternMatcher([pat])
        source = (
            "def foo(name):\n    cleaned = name.strip()\n    return cleaned + 'x'\n"
        )
        matches = matcher.find_matches(source, "test.py", "python")
        assert len(matches) == 1

    def test_str_methods_absent_in_enclosing_function_no_match(self):
        """Node inside a function with no str method → check fails."""
        pat = ASTPattern(
            id="test-strmethods-absent",
            language="python",
            node_type="BinOp",
            constraints={"context": {"function_uses_str_methods": True}},
        )
        matcher = ASTPatternMatcher([pat])
        source = "def foo(a, b):\n    return a + b\n"
        matches = matcher.find_matches(source, "test.py", "python")
        assert len(matches) == 0

    def test_str_methods_no_enclosing_function_no_match(self):
        """Module-level node (no enclosing function, not a def) → ``func_def``
        stays ``None`` and the guard returns False (line 297)."""
        pat = ASTPattern(
            id="test-strmethods-nofunc",
            language="python",
            node_type="BinOp",
            constraints={"context": {"function_uses_str_methods": True}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("x = 1 + 2\n", "test.py", "python")
        assert len(matches) == 0


# ── _check_ops: str-spec continuation in a chained compare (335->330) ──


class TestCheckOpsChainedCompareContinuation:
    """Covers branch ``335->330``.

    A chained comparison (``a < b < c``) produces two operators, so after the
    string-spec branch (line 335) matches the first operator and does *not*
    return, control loops back to the ``for`` header (line 330) for the next
    operator.
    """

    def test_two_string_ops_both_match(self):
        pat = ASTPattern(
            id="test-ops-str-chain",
            language="python",
            node_type="Compare",
            constraints={"ops": ["Lt", "Lt"]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if a < b < c:\n    pass\n", "test.py", "python")
        assert len(matches) == 1

    def test_two_string_ops_second_mismatch_no_match(self):
        pat = ASTPattern(
            id="test-ops-str-chain-miss",
            language="python",
            node_type="Compare",
            constraints={"ops": ["Lt", "Gt"]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if a < b < c:\n    pass\n", "test.py", "python")
        assert len(matches) == 0


class TestCheckOpsNonDictNonStrSpec:
    """Covers branch ``335->330`` — the elif-False arc of ``_check_ops``.

    When an ops spec entry is neither a ``dict`` nor a ``str`` (e.g. ``None``),
    both ``isinstance`` checks at lines 331 and 335 are False. Control skips the
    branch entirely and loops back to the ``for`` header (line 330). A ``None``
    sentinel mirrors the "don't constrain this position" convention already used
    by :meth:`_check_args` (line 353).
    """

    def test_none_spec_entry_is_treated_as_no_constraint(self):
        pat = ASTPattern(
            id="test-ops-none-spec",
            language="python",
            node_type="Compare",
            constraints={"ops": [None]},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches("if x > 5:\n    pass\n", "test.py", "python")
        assert len(matches) == 1


# ── _check_body_contains: dict spec without 'type' (370->377) ──


class TestCheckBodyContainsDictWithoutType:
    """Covers branch ``370->377``.

    A ``body_contains`` dict spec without a ``type`` key makes
    ``node_type_name`` falsy → skip the body walk → ``return False``.
    """

    def test_body_contains_empty_dict_no_match(self):
        pat = ASTPattern(
            id="test-body-no-type",
            language="python",
            node_type="FunctionDef",
            constraints={"body_contains": {}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return 1\n", "test.py", "python"
        )
        assert len(matches) == 0

    def test_body_contains_dict_with_only_extra_key_no_match(self):
        pat = ASTPattern(
            id="test-body-no-type-extra",
            language="python",
            node_type="FunctionDef",
            constraints={"body_contains": {"depth": 1}},
        )
        matcher = ASTPatternMatcher([pat])
        matches = matcher.find_matches(
            "def foo():\n    return 1\n", "test.py", "python"
        )
        assert len(matches) == 0
