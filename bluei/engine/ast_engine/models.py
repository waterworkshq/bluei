"""Data classes for AST patterns, matches, transforms, and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional



@dataclass
class ASTPattern:
    id: str
    language: str
    node_type: str
    constraints: Dict[str, Any] = field(default_factory=dict)
    negation_constraints: Optional[Dict[str, Any]] = None
    confidence: float = 0.80
    category: str = "lint"
    # Optional repository-relative paths (files or directories) this pattern
    # is scoped to. Fixture-derived domain rules (the qa-sandbox family) set
    # this so they never fire on unrelated repositories that merely contain
    # similar code. Matching is boundary-safe: "src/qa_sandbox/catalog.py"
    # does not match "src/qa_sandbox_evil/catalog.py".
    path_prefixes: Optional[List[str]] = None


@dataclass
class ASTMatch:
    pattern: ASTPattern
    node: Any
    line: int
    col: int
    source_text: str
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ASTTransform:
    id: str
    source_pattern_id: str
    transform_type: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ASTTransformResult:
    success: bool
    source: str = ""
    error: Optional[str] = None
    transform_id: Optional[str] = None
