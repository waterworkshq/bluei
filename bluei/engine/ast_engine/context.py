"""AST context queries — parent maps, scope lookups, enclosing function/class/loop."""

from __future__ import annotations

import ast
import re
from typing import Any, Dict, List, Optional, Tuple



def build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parent_map: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    return parent_map


def find_parent(node: ast.AST, parent_map: Dict[ast.AST, ast.AST]) -> Optional[ast.AST]:
    return parent_map.get(node)


def find_enclosing_function(
    node: ast.AST, parent_map: Dict[ast.AST, ast.AST]
) -> Optional[ast.FunctionDef]:
    current = parent_map.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parent_map.get(current)
    return None


def find_enclosing_class(
    node: ast.AST, parent_map: Dict[ast.AST, ast.AST]
) -> Optional[ast.ClassDef]:
    current = parent_map.get(node)
    while current is not None:
        if isinstance(current, ast.ClassDef):
            return current
        current = parent_map.get(current)
    return None


def find_enclosing_loop(
    node: ast.AST, parent_map: Dict[ast.AST, ast.AST]
) -> Optional[ast.AST]:
    current = parent_map.get(node)
    while current is not None:
        if isinstance(current, (ast.For, ast.AsyncFor, ast.While)):
            return current
        current = parent_map.get(current)
    return None


def extract_variable_names(node: ast.AST) -> List[str]:
    names: List[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.append(child.id)
    return names


def extract_method_chain(node: ast.AST) -> List[str]:
    chain: List[str] = []
    current = node
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        chain.append(current.func.attr)
        current = current.func.value
        if isinstance(current, ast.Call):
            continue
        elif isinstance(current, ast.Attribute):
            chain.append(current.attr)
            break
        elif isinstance(current, ast.Name):
            break
        else:
            break
    return chain


def extract_function_calls(func_def: ast.FunctionDef) -> List[str]:
    calls: List[str] = []
    for node in ast.walk(func_def):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
            elif isinstance(node.func, ast.Name):
                calls.append(node.func.id)
    return calls


def get_source_segment(source: str, node: ast.AST) -> str:
    if hasattr(ast, "get_source_segment"):
        seg = ast.get_source_segment(source, node)
        if seg is not None:
            return seg
    if hasattr(node, "lineno") and hasattr(node, "end_lineno"):
        lines = source.splitlines()
        if node.lineno and node.end_lineno:
            start = max(0, node.lineno - 1)
            end = node.end_lineno
            return "\n".join(lines[start:end])
    return ""


def is_in_return_statement(node: ast.AST, parent_map: Dict[ast.AST, ast.AST]) -> bool:
    parent = parent_map.get(node)
    while parent is not None:
        if isinstance(parent, ast.Return):
            return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        parent = parent_map.get(parent)
    return False


def is_in_except_handler(node: ast.AST, parent_map: Dict[ast.AST, ast.AST]) -> bool:
    parent = parent_map.get(node)
    while parent is not None:
        if isinstance(parent, ast.ExceptHandler):
            return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return False
        parent = parent_map.get(parent)
    return False


def get_function_param_names(func_def: ast.FunctionDef) -> List[str]:
    names: List[str] = []
    for arg in func_def.args.args:
        names.append(arg.arg)
    if func_def.args.vararg:
        names.append(func_def.args.vararg.arg)
    for arg in func_def.args.kwonlyargs:
        names.append(arg.arg)
    if func_def.args.kwarg:
        names.append(func_def.args.kwarg.arg)
    return names


def function_has_isinstance_guard(func_def: ast.FunctionDef) -> bool:
    for node in ast.walk(func_def):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "isinstance":
                return True
    return False


def get_caller_object(node: ast.Call) -> Optional[str]:
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return node.func.value.id
    return None
