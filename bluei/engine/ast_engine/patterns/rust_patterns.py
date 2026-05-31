"""Rust pattern definitions for tree-sitter matcher."""

from __future__ import annotations

from typing import List

from ..models import ASTPattern



def get_patterns() -> List[ASTPattern]:
    return RUST_PATTERNS


RUST_PATTERNS: List[ASTPattern] = [
    ASTPattern(
        id="rust-unwrap-panic",
        language="rust",
        node_type="call_expression",
        constraints={
            "rust_method_name": "unwrap",
        },
        negation_constraints={
            "in_test_file": True,
        },
        confidence=0.72,
        category="bug",
    ),
    ASTPattern(
        id="rust-clone-unnecessary",
        language="rust",
        node_type="call_expression",
        constraints={
            "rust_method_name": "clone",
        },
        confidence=0.60,
        category="perf-smell",
    ),
    ASTPattern(
        id="rust-expect-vague",
        language="rust",
        node_type="call_expression",
        constraints={
            "rust_method_name": "expect",
            "rust_arg_empty_or_vague": True,
        },
        confidence=0.70,
        category="lint",
    ),
]
