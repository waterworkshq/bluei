"""Walks parsed AST, checks patterns against constraint dicts, returns matched nodes."""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional

from .context import (

    build_parent_map,
    extract_function_calls,
    extract_method_chain,
    extract_variable_names,
    find_enclosing_function,
    find_enclosing_loop,
    find_parent,
    function_has_isinstance_guard,
    get_source_segment,
    is_in_except_handler,
    is_in_return_statement,
)
from .models import ASTMatch, ASTPattern


class ASTPatternMatcher:
    def __init__(self, patterns: Optional[List[ASTPattern]] = None):
        self.patterns: List[ASTPattern] = patterns or []

    def find_matches(
        self, source: str, file_path: str, language: str = "python"
    ) -> List[ASTMatch]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        parent_map = build_parent_map(tree)
        matches: List[ASTMatch] = []

        applicable = [p for p in self.patterns if p.language == language]
        for pattern in applicable:
            for node in ast.walk(tree):
                if self._matches_pattern(node, pattern, tree, parent_map, source):
                    if not self._post_filter(pattern.id, node, parent_map, source):
                        continue
                    source_text = get_source_segment(source, node)
                    line = getattr(node, "lineno", 0)
                    col = getattr(node, "col_offset", 0)
                    ctx = self._build_context(node, tree, parent_map)
                    matches.append(
                        ASTMatch(
                            pattern=pattern,
                            node=node,
                            line=line,
                            col=col,
                            source_text=source_text,
                            context=ctx,
                        )
                    )

        return matches

    def _post_filter(
        self,
        pattern_id: str,
        node: ast.AST,
        parent_map: Dict[ast.AST, ast.AST],
        source: str,
    ) -> bool:
        if pattern_id == "perf-pop-front-loop":
            if not isinstance(node, ast.Call):
                return False
            if not node.args:
                return False
            arg = node.args[0]
            if not isinstance(arg, ast.Constant) or arg.value != 0:
                return False
            loop = find_enclosing_loop(node, parent_map)
            return loop is not None

        if pattern_id == "perf-list-membership-loop":
            loop = find_enclosing_loop(node, parent_map)
            return loop is not None

        if pattern_id == "hardcoded-tmp-path":
            for arg in getattr(node, "args", []):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if "/tmp/" in arg.value:
                        return True
            return False

        if pattern_id == "hardcoded-tmp-path-string":
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                if "/tmp/" in val:
                    parent = find_parent(node, parent_map)
                    if isinstance(parent, ast.keyword):
                        return False
                    if isinstance(parent, ast.Call):
                        if isinstance(parent.func, ast.Name) and parent.func.id == "Path":
                            return False
                    return True
            return False

        if pattern_id == "broad-except":
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    return True
                if isinstance(node.type, ast.Name) and node.type.id == "Exception":
                    return True
                if isinstance(node.type, ast.Tuple):
                    return False
            return False

        return True

    def _matches_pattern(
        self,
        node: ast.AST,
        pattern: ASTPattern,
        tree: ast.AST,
        parent_map: Dict[ast.AST, ast.AST],
        source: str,
    ) -> bool:
        node_type = getattr(ast, pattern.node_type, None)
        if node_type is None:
            return False
        if not isinstance(node, node_type):
            return False

        for key, value in pattern.constraints.items():
            if not self._check_constraint(node, key, value, tree, parent_map, source):
                return False

        if pattern.negation_constraints:
            for key, value in pattern.negation_constraints.items():
                if self._check_constraint(node, key, value, tree, parent_map, source):
                    return False

        return True

    def _check_constraint(
        self,
        node: ast.AST,
        key: str,
        value: Any,
        tree: ast.AST,
        parent_map: Dict[ast.AST, ast.AST],
        source: str,
    ) -> bool:
        if key == "op":
            return self._check_op(node, value)
        elif key == "left":
            return self._check_node_spec(getattr(node, "left", None), value)
        elif key == "right":
            return self._check_node_spec(getattr(node, "right", None), value)
        elif key == "parent":
            return self._check_parent(node, value, parent_map)
        elif key == "context":
            return self._check_context(node, value, tree, parent_map)
        elif key == "func":
            return self._check_node_spec(getattr(node, "func", None), value)
        elif key == "ops":
            return self._check_ops(node, value)
        elif key == "test":
            return self._check_node_spec(getattr(node, "test", None), value)
        elif key == "type":
            return self._check_type_field(node, value)
        elif key == "args":
            return self._check_args(node, value)
        elif key == "body_contains":
            return self._check_body_contains(node, value, tree, parent_map)
        return False

    def _check_op(self, node: ast.AST, op_name: str) -> bool:
        op = getattr(node, "op", None)
        if op is None:
            return False
        op_type = getattr(ast, op_name, None)
        return isinstance(op, op_type) if op_type else False

    def _check_node_spec(self, node: Optional[ast.AST], spec: Any) -> bool:
        if node is None:
            return False
        if isinstance(spec, str):
            node_type = getattr(ast, spec, None)
            return isinstance(node, node_type) if node_type else False
        if isinstance(spec, dict):
            type_name = spec.get("type")
            if type_name:
                node_type = getattr(ast, type_name, None)
                if not node_type or not isinstance(node, node_type):
                    return False
            attr_name = spec.get("attr")
            if attr_name and hasattr(node, "attr"):
                if node.attr != attr_name:
                    return False
            id_name = spec.get("id")
            if id_name and hasattr(node, "id"):
                if node.id != id_name:
                    return False
            value_spec = spec.get("value")
            if value_spec is not None and hasattr(node, "value"):
                return self._check_node_spec(node.value, value_spec)
            return True
        return False

    def _check_parent(
        self, node: ast.AST, spec: Any, parent_map: Dict[ast.AST, ast.AST]
    ) -> bool:
        parent = find_parent(node, parent_map)
        if parent is None:
            return False
        if isinstance(spec, str):
            parent_type = getattr(ast, spec, None)
            return isinstance(parent, parent_type) if parent_type else False
        if isinstance(spec, dict):
            parent_type_name = spec.get("type")
            if parent_type_name:
                parent_type = getattr(ast, parent_type_name, None)
                if not parent_type or not isinstance(parent, parent_type):
                    return False
            depth = spec.get("depth", 1)
            current = parent
            for _ in range(depth - 1):
                current = find_parent(current, parent_map)
                if current is None:
                    return False
            return True
        return False

    def _check_context(
        self,
        node: ast.AST,
        context_spec: Dict[str, Any],
        tree: ast.AST,
        parent_map: Dict[ast.AST, ast.AST],
    ) -> bool:
        for key, value in context_spec.items():
            if key == "function_name_pattern":
                func_def = find_enclosing_function(node, parent_map)
                if func_def is None:
                    return False
                if not re.search(value, func_def.name):
                    return False

            elif key == "variables_include":
                names = extract_variable_names(node)
                if not any(
                    re.search(rf".*\b{re.escape(v)}\b.*", n, re.IGNORECASE)
                    for n in names
                    for v in value
                ):
                    return False

            elif key == "enclosing_function_calls_exclude":
                func_def = find_enclosing_function(node, parent_map)
                if func_def:
                    called = extract_function_calls(func_def)
                    if any(c in called for c in value):
                        return False

            elif key == "in_function_return":
                if not is_in_return_statement(node, parent_map):
                    return False

            elif key == "caller_chain_excludes":
                chain = extract_method_chain(node)
                if any(m in chain for m in value):
                    return False

            elif key == "in_loop_body":
                loop = find_enclosing_loop(node, parent_map)
                if loop is None:
                    return False

            elif key == "in_except_handler":
                if not is_in_except_handler(node, parent_map):
                    return False

            elif key == "function_has_isinstance_guard":
                func_def = find_enclosing_function(node, parent_map)
                if func_def is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_def = node
                if func_def is None:
                    return False
                has_guard = function_has_isinstance_guard(func_def)
                if value and has_guard:
                    return False

            elif key == "function_uses_str_methods":
                func_def = find_enclosing_function(node, parent_map)
                if func_def is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_def = node
                if func_def is None:
                    return False
                str_methods = {
                    "lower",
                    "upper",
                    "strip",
                    "lstrip",
                    "rstrip",
                    "capitalize",
                    "title",
                    "replace",
                    "split",
                    "startswith",
                    "endswith",
                }
                found = False
                for child in ast.walk(func_def):
                    if isinstance(child, ast.Call) and isinstance(
                        child.func, ast.Attribute
                    ):
                        if child.func.attr in str_methods:
                            found = True
                            break
                if not found:
                    return False

        return True

    def _check_ops(self, node: ast.AST, spec: list) -> bool:
        ops = getattr(node, "ops", None)
        if ops is None:
            return False
        if len(ops) != len(spec):
            return False
        for actual, expected in zip(ops, spec):
            if isinstance(expected, dict):
                expected_type = getattr(ast, expected.get("type", ""), None)
                if not isinstance(actual, expected_type):
                    return False
            elif isinstance(expected, str):
                expected_type = getattr(ast, expected, None)
                if not isinstance(actual, expected_type):
                    return False
        return True

    def _check_type_field(self, node: ast.AST, value: Any) -> bool:
        if isinstance(value, str):
            return getattr(node, "type", None) == value
        return False

    def _check_args(self, node: ast.AST, spec: list) -> bool:
        args = getattr(node, "args", None)
        if args is None:
            return False
        if len(spec) > len(args):
            return False
        for i, arg_spec in enumerate(spec):
            if arg_spec is not None:
                if not self._check_node_spec(args[i], arg_spec):
                    return False
        return True

    def _check_body_contains(
        self,
        node: ast.AST,
        spec: Any,
        tree: ast.AST,
        parent_map: Dict[ast.AST, ast.AST],
    ) -> bool:
        body = getattr(node, "body", None)
        if body is None:
            return False
        if isinstance(spec, dict):
            node_type_name = spec.get("type")
            if node_type_name:
                node_type = getattr(ast, node_type_name, None)
                for stmt in body:
                    for child in ast.walk(stmt):
                        if isinstance(child, node_type):
                            return True
                return False
        return False

    def _build_context(
        self, node: ast.AST, tree: ast.AST, parent_map: Dict[ast.AST, ast.AST]
    ) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        func_def = find_enclosing_function(node, parent_map)
        if func_def:
            ctx["enclosing_function"] = func_def.name
        class_def = find_enclosing_loop(node, parent_map)
        if class_def:
            ctx["in_loop"] = True
        return ctx

    def find_enclosing_loop(
        self, node: ast.AST, parent_map: Dict[ast.AST, ast.AST]
    ) -> Optional[ast.AST]:
        from .context import find_enclosing_loop as _find_loop

        return _find_loop(node, parent_map)
