"""AST-aware Structural Replay for seeded FixPatterns (ADR-0019).

When a Pattern's canonical ``before_snippet`` does not literally appear in
the target file (different variable names, imports, layout), Structural
Replay parses both snippets and the target into ASTs, structurally matches
them while building a variable-binding map, derives the AST edit from the
Pattern's before->after delta, applies the same edit to the matched target
subtree, and returns the new source.

Activated as a fallback inside ``PatternReplayCascadeStage`` on literal
miss. Python-only in alpha.4; TypeScript deferred.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

from bluei.engine.structural_hash import (
    fuzzy_structural_match,
    get_fuzzy_threshold,
)

if TYPE_CHECKING:
    from bluei.engine.pattern_store import FixPattern


@dataclass
class StructuralReplayResult:
    new_source: str
    matched_node_start_line: int
    matched_node_end_line: int
    binding_map: Dict[str, str] = field(default_factory=dict)
    fuzzy_score: float = 0.0
    imports_added: List[str] = field(default_factory=list)


def apply_structural_replay(
    file_text: str,
    pattern: "FixPattern",
    language: str,
    target_line: int = 0,
) -> Optional[StructuralReplayResult]:
    if language != "python":
        return None

    try:
        target_tree = ast.parse(file_text)
    except SyntaxError:
        return None

    try:
        before_tree = ast.parse(pattern.before_snippet)
        after_tree = ast.parse(pattern.after_snippet)
    except SyntaxError:
        return None

    if not before_tree.body or not after_tree.body:
        return None

    pattern_anchor = before_tree.body[0]
    after_anchor = after_tree.body[0]

    candidate = _locate_candidate(target_tree, file_text, target_line, pattern_anchor)
    if candidate is None:
        return None

    region_text = ast.get_source_segment(file_text, candidate) or file_text
    fuzzy = fuzzy_structural_match(region_text, pattern.before_snippet, language)
    if fuzzy < get_fuzzy_threshold(pattern.rule, catalog=_detector_catalog()):
        return None

    bindings: Dict[str, ast.AST] = {}
    if not _match(pattern_anchor, candidate, bindings):
        return None

    if not _bindings_complete(pattern_anchor, bindings):
        return None

    new_after = _apply_delta(after_anchor, bindings)

    imports_added = _compute_imports_added(
        before_tree, after_tree, pattern, target_tree
    )
    imports_to_insert = _parse_imports_for_insertion(imports_added, target_tree)

    new_target_tree = _SwapNode(candidate, new_after).visit(target_tree)
    if isinstance(new_target_tree, ast.Module) and imports_to_insert:
        new_target_tree.body = list(imports_to_insert) + list(new_target_tree.body)

    try:
        new_source = ast.unparse(new_target_tree)
        compile(new_source, "<structural-replay>", "exec")
    except SyntaxError:
        return None

    start_line = getattr(candidate, "lineno", 0) or 0
    end_line = getattr(candidate, "end_lineno", start_line) or start_line

    public_binding_map = {_public_key(k): _public_value(v) for k, v in bindings.items()}

    return StructuralReplayResult(
        new_source=new_source,
        matched_node_start_line=start_line,
        matched_node_end_line=end_line,
        binding_map=public_binding_map,
        fuzzy_score=fuzzy,
        imports_added=list(imports_added),
    )


def _detector_catalog() -> List[Dict]:
    try:
        from bluei.engine.constants import DETECTOR_CATALOG

        return DETECTOR_CATALOG
    except Exception:
        return []


def _locate_candidate(
    tree: ast.Module,
    file_text: str,
    target_line: int,
    pattern_anchor: ast.AST,
) -> Optional[ast.AST]:
    if target_line >= 1:
        return _smallest_stmt_at_line(tree, target_line)
    return _scan_for_structural_match(tree, pattern_anchor)


def _smallest_stmt_at_line(tree: ast.Module, target_line: int) -> Optional[ast.stmt]:
    candidates: List[ast.stmt] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue
        end_lineno = getattr(node, "end_lineno", None) or lineno
        if lineno <= target_line <= end_lineno:
            candidates.append(node)
    if not candidates:
        return None
    return min(
        candidates, key=lambda n: ((n.end_lineno or n.lineno) - n.lineno, n.lineno)
    )


def _scan_for_structural_match(
    tree: ast.Module, pattern_anchor: ast.AST
) -> Optional[ast.AST]:
    for node in ast.walk(tree):
        if type(node) is not type(pattern_anchor):
            continue
        probe: Dict[str, ast.AST] = {}
        if _match(pattern_anchor, node, probe) and _bindings_complete(
            pattern_anchor, probe
        ):
            return node
    return None


def _is_leaf(node: object) -> bool:
    return isinstance(node, (ast.Name, ast.Constant))


def _leaf_token(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return "N:" + node.id
    if isinstance(node, ast.Constant):
        return "C:" + repr(node.value)
    return ""


def _bind_leaf(
    pattern_leaf: ast.AST, target_leaf: ast.AST, bindings: Dict[str, ast.AST]
) -> bool:
    key = _leaf_token(pattern_leaf)
    if not key:
        return False
    existing = bindings.get(key)
    if existing is not None:
        return _leaf_token(existing) == _leaf_token(target_leaf)
    bindings[key] = copy.deepcopy(target_leaf)
    return True


def _match(node_a: ast.AST, node_b: ast.AST, bindings: Dict[str, ast.AST]) -> bool:
    if _is_leaf(node_a) and _is_leaf(node_b):
        return _bind_leaf(node_a, node_b, bindings)
    if type(node_a) is not type(node_b):
        return False
    for (_fa, va), (_fb, vb) in zip(ast.iter_fields(node_a), ast.iter_fields(node_b)):
        if not _match_field(va, vb, bindings):
            return False
    return True


def _match_field(va: object, vb: object, bindings: Dict[str, ast.AST]) -> bool:
    if va is None or vb is None:
        return va is None and vb is None
    if isinstance(va, ast.AST):
        if not isinstance(vb, ast.AST):
            return False
        return _match(va, vb, bindings)
    if isinstance(va, list):
        if not isinstance(vb, list):
            return False
        if len(va) != len(vb):
            return False
        for item_a, item_b in zip(va, vb):
            if not _match_field(item_a, item_b, bindings):
                return False
        return True
    return va == vb


def _bindings_complete(pattern_anchor: ast.AST, bindings: Dict[str, ast.AST]) -> bool:
    for node in ast.walk(pattern_anchor):
        if isinstance(node, ast.Name):
            if _leaf_token(node) not in bindings:
                return False
    return True


def _apply_delta(after_anchor: ast.AST, bindings: Dict[str, ast.AST]) -> ast.AST:
    applier = _DeltaApplier(bindings)
    return applier.visit(copy.deepcopy(after_anchor))


class _DeltaApplier(ast.NodeTransformer):
    def __init__(self, bindings: Dict[str, ast.AST]):
        self.bindings = bindings

    def visit_Name(self, node: ast.Name) -> ast.AST:
        key = _leaf_token(node)
        if key in self.bindings:
            return copy.deepcopy(self.bindings[key])
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        key = _leaf_token(node)
        if key in self.bindings:
            return copy.deepcopy(self.bindings[key])
        return node


class _SwapNode(ast.NodeTransformer):
    def __init__(self, target: ast.AST, new_node: ast.AST):
        self.target = target
        self.new_node = new_node
        self.found = False

    def visit(self, node: ast.AST) -> ast.AST:
        if node is self.target:
            self.found = True
            return self.new_node
        return super().visit(node)


def _collect_imports(tree: ast.Module) -> List[str]:
    result: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.append(ast.unparse(node))
    return result


def _normalize_import_text(text: str) -> str:
    try:
        parsed = ast.parse(text)
        if parsed.body and isinstance(parsed.body[0], (ast.Import, ast.ImportFrom)):
            return ast.unparse(parsed.body[0])
    except SyntaxError:
        pass
    return text.strip()


def _compute_imports_added(
    before_tree: ast.Module,
    after_tree: ast.Module,
    pattern: "FixPattern",
    target_tree: ast.Module,
) -> List[str]:
    before_imports = set(_collect_imports(before_tree))
    after_imports = _collect_imports(after_tree)
    touched = {_normalize_import_text(t) for t in pattern.imports_touched}

    added: List[str] = []
    for imp in after_imports:
        if imp in before_imports:
            continue
        if touched and imp not in touched:
            continue
        added.append(imp)
    return added


def _parse_imports_for_insertion(
    imports_added: List[str], target_tree: ast.Module
) -> List[ast.stmt]:
    existing_target_imports = set(_collect_imports(target_tree))
    inserted: List[ast.stmt] = []
    for imp_text in imports_added:
        if imp_text in existing_target_imports:
            continue
        try:
            parsed = ast.parse(imp_text)
        except SyntaxError:
            continue
        for stmt in parsed.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                inserted.append(stmt)
    return inserted


def _public_key(internal_key: str) -> str:
    if internal_key.startswith("N:"):
        return internal_key[2:]
    if internal_key.startswith("C:"):
        return internal_key[2:]
    return internal_key


def _public_value(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ast.unparse(node)
