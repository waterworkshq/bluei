"""TypeScript/JavaScript pattern definitions for tree-sitter matcher."""

from __future__ import annotations

from typing import List

from ..models import ASTPattern



def get_patterns() -> List[ASTPattern]:
    return TS_PATTERNS


TS_PATTERNS: List[ASTPattern] = [
    ASTPattern(
        id="type-explicit-any",
        language="typescript",
        node_type="type_annotation",
        constraints={
            "ts_node_child_type": "predefined_type",
            "ts_node_child_text": "any",
        },
        confidence=0.85,
        category="type-safety",
    ),
    ASTPattern(
        id="debug-console-log",
        language="typescript",
        node_type="call_expression",
        constraints={
            "ts_call_name": "console.log",
        },
        negation_constraints={
            "in_test_file": True,
        },
        confidence=0.70,
        category="lint",
    ),
    ASTPattern(
        id="ts-unsafe-any-cast",
        language="typescript",
        node_type="as_expression",
        constraints={
            "ts_right_type": "any",
        },
        confidence=0.82,
        category="type-safety",
    ),
    ASTPattern(
        id="type-missing-return",
        language="typescript",
        node_type="function_declaration",
        constraints={
            "ts_missing_return_type": True,
            "ts_is_exported": True,
        },
        confidence=0.80,
        category="type-safety",
    ),
    ASTPattern(
        id="xo-no-warning-comments",
        language="typescript",
        node_type="comment",
        constraints={
            "ts_comment_pattern": "TODO|FIXME|HACK|XXX",
        },
        negation_constraints={
            "in_test_file": True,
        },
        confidence=0.80,
        category="lint",
    ),
    ASTPattern(
        id="ts-unhandled-promise",
        language="typescript",
        node_type="call_expression",
        constraints={
            "ts_call_returns_promise": True,
        },
        negation_constraints={
            "ts_has_catch": True,
            "ts_is_awaited": True,
        },
        confidence=0.78,
        category="bug",
    ),
]
