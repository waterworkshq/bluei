"""Go pattern definitions for tree-sitter matcher."""

from __future__ import annotations

from typing import List

from ..models import ASTPattern



def get_patterns() -> List[ASTPattern]:
    return GO_PATTERNS


GO_PATTERNS: List[ASTPattern] = [
    ASTPattern(
        id="go-unchecked-error",
        language="go",
        node_type="short_var_declaration",
        constraints={
            "go_rhs_is_call": True,
        },
        negation_constraints={
            "go_lhs_has_error_var": True,
        },
        confidence=0.75,
        category="bug",
    ),
    ASTPattern(
        id="go-empty-interface",
        language="go",
        node_type="interface_type",
        constraints={
            "go_interface_empty": True,
        },
        confidence=0.70,
        category="lint",
    ),
    ASTPattern(
        id="go-goroutine-unsafe",
        language="go",
        node_type="go_statement",
        constraints={},
        negation_constraints={
            "go_has_defer_recover": True,
        },
        confidence=0.65,
        category="bug",
    ),
]
