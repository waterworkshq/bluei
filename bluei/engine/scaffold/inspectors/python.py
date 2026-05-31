"""scaffold.inspectors.python — Python module inspector using stdlib ast."""

from __future__ import annotations

import logging
import ast
from pathlib import Path
from typing import List, Optional

_logger = logging.getLogger(__name__)

from ..models import ClassExport, FunctionExport, ModuleExports, ParamInfo


def inspect_python_module(file_path: Path, worktree: Optional[Path] = None) -> ModuleExports:
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))

    module_name = _infer_module_name(file_path, worktree)
    functions: List[FunctionExport] = []
    classes: List[ClassExport] = []
    constants: List[str] = []
    imports: List[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                functions.append(_extract_function(node))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                classes.append(_extract_class(node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    constants.append(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                constants.append(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(_format_import(node))

    return ModuleExports(
        path=str(file_path),
        module_name=module_name,
        functions=functions,
        classes=classes,
        constants=constants,
        imports=imports,
    )


def _extract_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionExport:
    params = _extract_params(node.args)
    return_type = _annotation_str(node.returns) if node.returns else None
    docstring = ast.get_docstring(node)
    decorators = [_format_decorator(d) for d in node.decorator_list]
    is_async = isinstance(node, ast.AsyncFunctionDef)

    return FunctionExport(
        name=node.name,
        params=params,
        return_type=return_type,
        docstring=docstring,
        decorators=decorators,
        is_async=is_async,
    )


def _extract_params(args: ast.arguments) -> List[ParamInfo]:
    params: List[ParamInfo] = []
    defaults_offset = len(args.args) - len(args.defaults)

    for i, arg in enumerate(args.args):
        if arg.arg == "self" or arg.arg == "cls":
            continue
        type_annotation = _annotation_str(arg.annotation) if arg.annotation else None
        default = None
        default_idx = i - defaults_offset
        if default_idx >= 0 and default_idx < len(args.defaults):
            default = ast.unparse(args.defaults[default_idx])
        params.append(ParamInfo(name=arg.arg, type_annotation=type_annotation, default=default))

    if args.vararg:
        params.append(ParamInfo(name=f"*{args.vararg.arg}"))
    if args.kwonlyargs:
        for arg in args.kwonlyargs:
            type_annotation = _annotation_str(arg.annotation) if arg.annotation else None
            params.append(ParamInfo(name=arg.arg, type_annotation=type_annotation))
    if args.kwarg:
        params.append(ParamInfo(name=f"**{args.kwarg.arg}"))

    return params


def _extract_class(node: ast.ClassDef) -> ClassExport:
    bases = [ast.unparse(b) for b in node.bases]
    methods: List[FunctionExport] = []
    is_abstract = False

    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name != "__init__":
                continue
            func = _extract_function(item)
            methods.append(func)
            if item.name == "__init__":
                func.name = "__init__"

    for decorator in node.decorator_list:
        dec_str = _format_decorator(decorator)
        if "abc.ABC" in dec_str or "ABCMeta" in dec_str:
            is_abstract = True

    for base in bases:
        if "ABC" in base:
            is_abstract = True

    return ClassExport(
        name=node.name,
        bases=bases,
        methods=methods,
        is_abstract=is_abstract,
    )


def _annotation_str(annotation: ast.expr) -> str:
    return ast.unparse(annotation)


def _format_decorator(decorator: ast.expr) -> str:
    return ast.unparse(decorator)


def _format_import(node: ast.Import | ast.ImportFrom) -> str:
    return ast.unparse(node)


def _infer_module_name(file_path: Path, worktree: Optional[Path] = None) -> str:
    parts = file_path.parts

    if worktree:
        try:
            relative = file_path.relative_to(worktree)
            parts = relative.parts
        except ValueError:
            _logger.debug("Failed to compute relative path")

    src_idx = None
    for i, p in enumerate(parts):
        if p == "src":
            src_idx = i
            break

    if src_idx is not None:
        module_parts = parts[src_idx + 1:]
    else:
        module_parts = parts

    module_parts = tuple(p for p in module_parts if p not in ("__init__.py",))

    if module_parts:
        last = module_parts[-1]
        if last.endswith(".py"):
            module_parts = module_parts[:-1] + (last[:-3],)

    return ".".join(module_parts)
