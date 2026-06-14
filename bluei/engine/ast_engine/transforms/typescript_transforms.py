"""TypeScript transform definitions — stubs for tree-sitter-based application.

These transform definitions describe the intended fix for each TypeScript
AST pattern. Application of these transforms requires the tree-sitter-based
transformer (ts_transformer.py), which is Phase 2 work per the plan.
For now, the definitions are loadable and discoverable but not applied.
"""

from __future__ import annotations

from typing import List

from ..models import ASTTransform


def get_transforms() -> List[ASTTransform]:
    return TS_TRANSFORMS


TS_TRANSFORMS: List[ASTTransform] = [
    ASTTransform(
        id="fix-type-explicit-any",
        source_pattern_id="type-explicit-any",
        transform_type="replace_op",
        params={"old": "any", "new": "unknown"},
    ),
    ASTTransform(
        id="fix-debug-console-log",
        source_pattern_id="debug-console-log",
        transform_type="remove_call",
        params={"function": "console.log"},
    ),
    ASTTransform(
        id="fix-ts-unsafe-any-cast",
        source_pattern_id="ts-unsafe-any-cast",
        transform_type="replace_op",
        params={"old": "as any", "new": "as unknown"},
    ),
    ASTTransform(
        id="fix-type-missing-return",
        source_pattern_id="type-missing-return",
        transform_type="insert_annotation",
        params={"annotation": ": void"},
    ),
    ASTTransform(
        id="fix-xo-no-warning-comments",
        source_pattern_id="xo-no-warning-comments",
        transform_type="remove_comment",
        params={},
    ),
    ASTTransform(
        id="fix-ts-unhandled-promise",
        source_pattern_id="ts-unhandled-promise",
        transform_type="wrap_await",
        params={},
    ),
]
