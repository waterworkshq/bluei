"""Go transform definitions — stubs for tree-sitter-based application.

These transform definitions describe the intended fix for each Go AST
pattern. Application of these transforms requires the tree-sitter-based
transformer (go_transformer.py), which is Phase 2 work per the plan.
For now, the definitions are loadable and discoverable but not applied.
"""

from __future__ import annotations

from typing import List

from ..models import ASTTransform


def get_transforms() -> List[ASTTransform]:
    return GO_TRANSFORMS


GO_TRANSFORMS: List[ASTTransform] = [
    ASTTransform(
        id="fix-go-unchecked-error",
        source_pattern_id="go-unchecked-error",
        transform_type="wrap_error_check",
        params={"pattern": "if err != nil { return err }"},
    ),
    ASTTransform(
        id="fix-go-empty-interface",
        source_pattern_id="go-empty-interface",
        transform_type="replace_op",
        params={"old": "interface{}", "new": "any"},
    ),
    ASTTransform(
        id="fix-go-goroutine-unsafe",
        source_pattern_id="go-goroutine-unsafe",
        transform_type="insert_synchronization",
        params={"sync_primitive": "sync.WaitGroup"},
    ),
]
