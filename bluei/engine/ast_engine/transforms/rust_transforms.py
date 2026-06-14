"""Rust transform definitions — stubs for tree-sitter-based application.

These transform definitions describe the intended fix for each Rust AST
pattern. Application of these transforms requires the tree-sitter-based
transformer (rust_transformer.py), which is Phase 2 work per the plan.
For now, the definitions are loadable and discoverable but not applied.
"""

from __future__ import annotations

from typing import List

from ..models import ASTTransform


def get_transforms() -> List[ASTTransform]:
    return RUST_TRANSFORMS


RUST_TRANSFORMS: List[ASTTransform] = [
    ASTTransform(
        id="fix-rust-unwrap-panic",
        source_pattern_id="rust-unwrap-panic",
        transform_type="wrap_error_check",
        params={"old": "unwrap()", "new": "ok_or_else(|| /* handle error */)?"},
    ),
    ASTTransform(
        id="fix-rust-clone-unnecessary",
        source_pattern_id="rust-clone-unnecessary",
        transform_type="remove_call",
        params={"method": "clone"},
    ),
    ASTTransform(
        id="fix-rust-expect-vague",
        source_pattern_id="rust-expect-vague",
        transform_type="replace_op",
        params={"old": 'expect("ok")', "new": 'expect("specific error message")'},
    ),
]
