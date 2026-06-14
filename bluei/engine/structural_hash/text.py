"""Language-agnostic text normalization helpers.

Self-contained — no imports from sibling modules. Used by `python.py`
for the actual normalization step after AST traversal, and re-exported
by the package `__init__.py` for backward compatibility.
"""

from __future__ import annotations

import hashlib
import re
from typing import List


def _text_structural_hash(code: str) -> str:
    normalized = _text_normalize(code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _text_normalize(code: str) -> str:
    text = code
    text = re.sub(r"#.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '"<str>"', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "'<str>'", text)
    text = re.sub(r"\b\d+\.?\d*\b", "<num>", text)
    text = re.sub(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", "<id>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _text_node_sequence(code: str) -> List[str]:
    tokens = []
    for m in re.finditer(r"[a-zA-Z_]\w*|[+\-*/%=<>!&|^~]+|[{}\[\](),.;:]", code):
        tok = m.group()
        if re.match(r"^[a-zA-Z_]", tok):
            tokens.append("ID")
        else:
            tokens.append(tok)
    return tokens


def _text_operator_sequence(code: str) -> List[str]:
    ops = []
    for m in re.finditer(r"[+\-*/%=<>!&|^~]+", code):
        ops.append(m.group())
    return ops


def _text_normalize_for_sharing(code: str) -> str:
    return _text_normalize(code)


def _levenshtein_distance(a: List[str], b: List[str]) -> int:
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)
    for i in range(1, la + 1):
        curr[0] = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + cost,
            )
        prev, curr = curr, prev
    return prev[lb]
