"""Structural code fingerprinting for duplicate/similar pattern detection.

Normalizes AST and text to structure-only tokens, then hashes for exact
matching or computes similarity via Levenshtein distance. Used by the
pattern replay system.

Public API:
    - compute_structural_hash(code, language) -> str
    - extract_node_sequence(code, language) -> List[str]
    - extract_operator_sequence(code, language) -> List[str]
    - sequence_similarity(seq_a, seq_b) -> float
    - fuzzy_structural_match(finding_snippet, pattern_before, language) -> float
    - get_fuzzy_threshold(rule, catalog=None) -> float
    - normalize_for_sharing(code, language) -> str
    - FUZZY_THRESHOLDS, DEFAULT_FUZZY_THRESHOLD

Language-specific implementation lives in `.python` and `.text`.
Private symbols are re-exported here for backward compatibility with
existing tests and patch sites.
"""

from typing import Dict, List, Optional

from bluei.engine.structural_hash.python import (
    _NodeSequenceExtractor,
    _OperatorExtractor,
    _PythonStructuralNormalizer,
    _SharingNormalizer,
    _python_normalize_for_sharing,
    _python_node_sequence,
    _python_operator_sequence,
    _python_structural_hash,
)
from bluei.engine.structural_hash.text import (
    _levenshtein_distance,
    _text_normalize,
    _text_normalize_for_sharing,
    _text_node_sequence,
    _text_operator_sequence,
    _text_structural_hash,
)

FUZZY_THRESHOLDS: Dict[str, float] = {
    "bug": 0.85,
    "lint": 0.70,
    "type-safety": 0.80,
    "test-coverage": 0.60,
    "test-gap": 0.60,
    "refactor": 0.75,
    "perf-smell": 0.80,
    "security": 0.85,
    "style": 0.65,
    "docs-gap": 0.65,
    "docs-drift": 0.65,
    "docs-mismatch": 0.65,
    "todo/debt": 0.70,
}

DEFAULT_FUZZY_THRESHOLD = 0.75


def compute_structural_hash(code: str, language: str) -> str:
    if language in ("python",):
        return _python_structural_hash(code)
    return _text_structural_hash(code)


def extract_node_sequence(code: str, language: str) -> List[str]:
    if language in ("python",):
        return _python_node_sequence(code)
    return _text_node_sequence(code)


def extract_operator_sequence(code: str, language: str) -> List[str]:
    if language in ("python",):
        return _python_operator_sequence(code)
    return _text_operator_sequence(code)


def sequence_similarity(seq_a: List[str], seq_b: List[str]) -> float:
    if not seq_a and not seq_b:
        return 1.0
    if not seq_a or not seq_b:
        return 0.0
    la, lb = len(seq_a), len(seq_b)
    if abs(la - lb) > max(la, lb):
        return 0.0
    dist = _levenshtein_distance(seq_a, seq_b)
    max_len = max(la, lb)
    if max_len == 0:
        return 1.0
    return 1.0 - (dist / max_len)


def fuzzy_structural_match(
    finding_snippet: str,
    pattern_before: str,
    language: str,
) -> float:
    h1 = compute_structural_hash(finding_snippet, language)
    h2 = compute_structural_hash(pattern_before, language)
    if h1 == h2:
        return 1.0
    nodes_a = extract_node_sequence(finding_snippet, language)
    nodes_b = extract_node_sequence(pattern_before, language)
    node_sim = sequence_similarity(nodes_a, nodes_b)
    ops_a = extract_operator_sequence(finding_snippet, language)
    ops_b = extract_operator_sequence(pattern_before, language)
    if ops_a or ops_b:
        op_sim = sequence_similarity(ops_a, ops_b)
        return 0.4 * node_sim + 0.6 * op_sim
    return node_sim


def get_fuzzy_threshold(rule: str, catalog: Optional[List[Dict]] = None) -> float:
    if catalog:
        for entry in catalog:
            if entry.get("rule") == rule:
                cat = entry.get("category", "")
                return FUZZY_THRESHOLDS.get(cat, DEFAULT_FUZZY_THRESHOLD)
    return DEFAULT_FUZZY_THRESHOLD


def normalize_for_sharing(code: str, language: str) -> str:
    if language in ("python",):
        return _python_normalize_for_sharing(code)
    return _text_normalize_for_sharing(code)


__all__ = [
    "compute_structural_hash",
    "extract_node_sequence",
    "extract_operator_sequence",
    "sequence_similarity",
    "fuzzy_structural_match",
    "get_fuzzy_threshold",
    "normalize_for_sharing",
    "FUZZY_THRESHOLDS",
    "DEFAULT_FUZZY_THRESHOLD",
    # Backward-compat private re-exports (used by tests + patch sites)
    "_python_structural_hash",
    "_python_node_sequence",
    "_python_operator_sequence",
    "_python_normalize_for_sharing",
    "_text_structural_hash",
    "_text_normalize",
    "_text_node_sequence",
    "_text_operator_sequence",
    "_text_normalize_for_sharing",
    "_levenshtein_distance",
    "_PythonStructuralNormalizer",
    "_NodeSequenceExtractor",
    "_OperatorExtractor",
    "_SharingNormalizer",
]
