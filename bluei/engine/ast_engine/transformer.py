"""Re-parses source, mutates AST nodes per transform ops, unparses and validates."""

from __future__ import annotations

import ast
from typing import Dict, List, Optional

from .context import build_parent_map, find_enclosing_function
from .models import ASTMatch, ASTTransform, ASTTransformResult


class ASTTransformer:
    def __init__(self, transforms: Optional[List[ASTTransform]] = None):
        self.transforms: List[ASTTransform] = transforms or []
        self._by_pattern: Dict[str, ASTTransform] = {}
        for t in self.transforms:
            self._by_pattern[t.source_pattern_id] = t

    def get_transform(self, pattern_id: str) -> Optional[ASTTransform]:
        return self._by_pattern.get(pattern_id)

    def apply(
        self, source: str, match: ASTMatch, transform: ASTTransform
    ) -> ASTTransformResult:
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return ASTTransformResult(success=False, source=source, error=str(e))

        target = self._find_node_in_tree(tree, match)
        if target is None:
            return ASTTransformResult(
                success=False, source=source, error="Target node not found in tree"
            )

        handler = getattr(self, f"_transform_{transform.transform_type}", None)
        if handler is None:
            return ASTTransformResult(
                success=False,
                source=source,
                error=f"Unknown transform type: {transform.transform_type}",
            )

        try:
            handler(tree, target, transform.params)
        except Exception as e:
            return ASTTransformResult(success=False, source=source, error=str(e))

        try:
            new_source = ast.unparse(tree)
        except Exception as e:
            return ASTTransformResult(success=False, source=source, error=str(e))

        try:
            compile(new_source, "<ast-transform>", "exec")
        except SyntaxError as e:
            return ASTTransformResult(
                success=False, source=new_source, error=f"Invalid output: {e}"
            )

        return ASTTransformResult(
            success=True, source=new_source, transform_id=transform.id
        )

    def _find_node_in_tree(self, tree: ast.AST, match: ASTMatch) -> Optional[ast.AST]:
        match_line = match.line
        match_col = match.col

        if match.node is not None:
            match_type = type(match.node)
            for node in ast.walk(tree):
                if isinstance(node, match_type):
                    if (
                        getattr(node, "lineno", None) == match_line
                        and getattr(node, "col_offset", None) == match_col
                    ):
                        return node

        match_text = (match.source_text or "").strip()
        if match_line and match_text:
            for node in ast.walk(tree):
                lineno = getattr(node, "lineno", None)
                if lineno != match_line:
                    continue
                end_lineno = getattr(node, "end_lineno", None)
                if end_lineno and end_lineno != match_line:
                    continue
                node_text = (
                    ast.get_source_segment(
                        ast.get_source_segment(tree, node) or "", node
                    )
                    if hasattr(ast, "get_source_segment")
                    else None
                )
                if node_text and node_text.strip() == match_text:
                    return node

        if match_line and not match_text:
            for node in ast.walk(tree):
                if getattr(node, "lineno", None) == match_line:
                    return node

        return None

    def _transform_replace_op(self, tree: ast.AST, node: ast.AST, params: Dict) -> None:
        op_map = {
            "Add": ast.Add(),
            "Sub": ast.Sub(),
            "Mult": ast.Mult(),
            "Div": ast.Div(),
            "Mod": ast.Mod(),
            "Pow": ast.Pow(),
        }
        if isinstance(node, ast.BinOp):
            old_name = type(node.op).__name__
            if old_name == params.get("old_op"):
                new_op = op_map.get(params.get("new_op"))
                if new_op:
                    node.op = new_op

    def _transform_prepend_method(
        self, tree: ast.AST, node: ast.AST, params: Dict
    ) -> None:
        method = params.get("method", "strip")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            inner_call = ast.Call(
                func=ast.Attribute(
                    value=node.func.value,
                    attr=method,
                ),
                args=[],
                keywords=[],
            )
            ast.fix_missing_locations(inner_call)
            node.func.value = inner_call

    def _transform_insert_guard(
        self, tree: ast.AST, node: ast.AST, params: Dict
    ) -> None:
        variable = params.get("variable", "value")
        check_type = params.get("check_type", "str")
        message = params.get("message", f"Expected {check_type}")

        func_def = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_def = node
        else:
            parent_map = build_parent_map(tree)
            func_def = find_enclosing_function(node, parent_map)
        if func_def is None:
            return

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
        insert_idx = 0
        for i, body_stmt in enumerate(func_def.body):
            for child in ast.walk(body_stmt):
                if isinstance(child, ast.Call) and isinstance(
                    child.func, ast.Attribute
                ):
                    if child.func.attr in str_methods:
                        insert_idx = i
                        break
            if insert_idx > 0:
                break

        if isinstance(func_def.body[0], ast.Expr) and isinstance(
            func_def.body[0].value, ast.Constant
        ):
            if isinstance(func_def.body[0].value.value, str):
                insert_idx = max(insert_idx, 1)

        guard = ast.If(
            test=ast.UnaryOp(
                op=ast.Not(),
                operand=ast.Call(
                    func=ast.Name(id="isinstance"),
                    args=[
                        ast.Name(id=variable),
                        ast.Name(id=check_type),
                    ],
                    keywords=[],
                ),
            ),
            body=[
                ast.Raise(
                    exc=ast.Call(
                        func=ast.Name(id="TypeError"),
                        args=[ast.Constant(value=message)],
                        keywords=[],
                    ),
                ),
            ],
            orelse=[],
        )
        ast.fix_missing_locations(guard)
        func_def.body.insert(insert_idx, guard)

    def _transform_wrap_set(self, tree: ast.AST, node: ast.AST, params: Dict) -> None:
        if isinstance(node, ast.Compare):
            new_comparators = []
            for comp in node.comparators:
                if isinstance(comp, ast.Name):
                    set_call = ast.Call(
                        func=ast.Name(id="set"),
                        args=[comp],
                        keywords=[],
                    )
                    ast.fix_missing_locations(set_call)
                    new_comparators.append(set_call)
                else:
                    new_comparators.append(comp)
            node.comparators = new_comparators

    def _transform_replace_path(
        self, tree: ast.AST, node: ast.AST, params: Dict
    ) -> None:
        old_prefix = params.get("old_prefix", "/tmp/")
        new_prefix = params.get("new_prefix", "/var/lib/")
        if isinstance(node, ast.Call):
            for i, arg in enumerate(node.args):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if old_prefix in arg.value:
                        node.args[i] = ast.Constant(
                            value=arg.value.replace(old_prefix, new_prefix)
                        )
                        ast.fix_missing_locations(node.args[i])

    def _transform_wrap_normalize(
        self, tree: ast.AST, node: ast.AST, params: Dict
    ) -> None:
        methods = params.get("methods", ["strip", "lower"])
        if isinstance(node, ast.Compare):
            new_left = self._wrap_in_methods(node.left, methods)
            new_comparators = []
            for comp in node.comparators:
                new_comparators.append(self._wrap_in_methods(comp, methods))
            node.left = new_left
            node.comparators = new_comparators

    def _wrap_in_methods(self, node: ast.AST, methods: list) -> ast.AST:
        current = node
        for method in methods:
            call = ast.Call(
                func=ast.Attribute(value=current, attr=method),
                args=[],
                keywords=[],
            )
            ast.fix_missing_locations(call)
            current = call
        return current

    def _contains_node(self, container: ast.AST, target: ast.AST) -> bool:
        target_line = getattr(target, "lineno", None)
        target_col = getattr(target, "col_offset", None)
        for node in ast.walk(container):
            if node is target:
                return True
            if (
                target_line is not None
                and getattr(node, "lineno", None) == target_line
                and target_col is not None
                and getattr(node, "col_offset", None) == target_col
                and type(node) == type(target)
            ):
                return True
        return False
