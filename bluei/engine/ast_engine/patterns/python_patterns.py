"""Python AST pattern definitions — dicts consumed by matcher."""

from __future__ import annotations

from typing import List

from ..models import ASTPattern



def get_patterns() -> List[ASTPattern]:
    return PYTHON_PATTERNS


PYTHON_PATTERNS: List[ASTPattern] = [
    ASTPattern(
        id="perf-pop-front-loop",
        language="python",
        node_type="Call",
        constraints={
            "func": {"type": "Attribute", "attr": "pop"},
        },
        negation_constraints=None,
        confidence=0.83,
        category="perf-smell",
    ),
    ASTPattern(
        id="perf-list-membership-loop",
        language="python",
        node_type="Compare",
        constraints={
            "ops": [{"type": "In"}],
        },
        confidence=0.82,
        category="perf-smell",
    ),
    ASTPattern(
        id="notifications-email-no-trim",
        language="python",
        node_type="Call",
        constraints={
            "func": {"type": "Attribute", "attr": "lower"},
            "context": {
                "in_function_return": True,
                "caller_chain_excludes": ["strip"],
            },
        },
        confidence=0.89,
        category="bug",
    ),
    ASTPattern(
        id="notifications-type-guard-missing",
        language="python",
        node_type="FunctionDef",
        constraints={
            "context": {
                "function_uses_str_methods": True,
                "function_has_isinstance_guard": True,
            },
        },
        confidence=0.87,
        category="bug",
    ),
    ASTPattern(
        id="hardcoded-tmp-path",
        language="python",
        node_type="Call",
        constraints={
            "func": {"type": "Name", "id": "Path"},
        },
        confidence=0.81,
        category="lint",
    ),
    ASTPattern(
        id="hardcoded-tmp-path-string",
        language="python",
        node_type="Constant",
        constraints={},
        confidence=0.81,
        category="lint",
    ),
    ASTPattern(
        id="catalog-query-not-normalized",
        language="python",
        node_type="Compare",
        constraints={
            "ops": [{"type": "Eq"}],
            "context": {
                "enclosing_function_calls_exclude": [
                    "strip",
                    "lower",
                    "upper",
                    "normalize",
                    "casefold",
                ],
            },
        },
        confidence=0.93,
        category="bug",
    ),
    ASTPattern(
        id="discount-math-sign",
        language="python",
        node_type="BinOp",
        constraints={
            "op": "Add",
            "context": {
                "function_name_pattern": r".*(?:discount|price|calc|total|amount|rebate|coupon).*",
                "in_function_return": True,
            },
        },
        confidence=0.95,
        category="bug",
    ),
    ASTPattern(
        id="broad-except",
        language="python",
        node_type="ExceptHandler",
        constraints={},
        confidence=0.88,
        category="lint",
    ),
]
