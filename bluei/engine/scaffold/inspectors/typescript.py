"""scaffold.inspectors.typescript — TypeScript module inspector (regex-based)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

_logger = logging.getLogger(__name__)

from ..models import ClassExport, FunctionExport, ModuleExports, ParamInfo


_FUNCTION_RE = re.compile(
    r"^export\s+(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)\s*(?::\s*([^{]+?))?\s*\{",
    re.MULTILINE,
)

_ARROW_FUNCTION_RE = re.compile(
    r"^export\s+const\s+(\w+)\s*(?::\s*[^=]+?)?\s*=\s*(async\s+)?\(([^)]*)\)\s*(?::\s*([^{]+?))?\s*=>",
    re.MULTILINE,
)

_CLASS_DECL_RE = re.compile(
    r"^export\s+(?:abstract\s+)?class\s+(\w+)(?:\s+extends\s+([\w<>,\s]+?))?"
    r"(?:\s+implements\s+([^{]+?))?\s*\{",
    re.MULTILINE,
)

_CLASS_METHOD_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|readonly\s+|static\s+)*"
    r"(?:async\s+)?(\w+)\s*\(([^)]*)\)\s*(?::\s*([^{]+?))?\s*\{",
    re.MULTILINE,
)

_CONSTANT_RE = re.compile(
    r"^export\s+const\s+(\w+)\s*(?::\s*[^=]+?)?\s*=",
    re.MULTILINE,
)

_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:type\s+)?(?:\{([^}]+)\}|(\*\s+as\s+\w+)|(\w+))"
    r"(?:\s*,\s*\{([^}]+)\})?\s*from\s+['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)

_SIMPLE_TYPE_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "do"}


def inspect_typescript_module(
    file_path: Path, worktree: Optional[Path] = None
) -> ModuleExports:
    """Inspect a TypeScript module and return its public exports.

    Uses regex-based extraction (tree-sitter is Phase 2).
    Handles: functions, async functions, arrow functions, classes, constants, imports.
    """
    source = file_path.read_text(encoding="utf-8")
    module_name = _infer_module_name(file_path, worktree)

    functions: List[FunctionExport] = []
    classes: List[ClassExport] = []
    constants: List[str] = []
    imports: List[str] = []
    arrow_names: set[str] = set()

    for match in _ARROW_FUNCTION_RE.finditer(source):
        name = match.group(1)
        if name.startswith("_"):
            continue
        arrow_names.add(name)
        params_str = match.group(3) or ""
        return_type = _clean_type(match.group(4))
        is_async = bool(match.group(2))
        functions.append(
            FunctionExport(
                name=name,
                params=_parse_params(params_str),
                return_type=return_type,
                docstring=None,
                decorators=[],
                is_async=is_async,
            )
        )

    for match in _FUNCTION_RE.finditer(source):
        name = match.group(1)
        if name.startswith("_"):
            continue
        params_str = match.group(2) or ""
        return_type = _clean_type(match.group(3))
        is_async = "async function" in match.group(0)
        decorators = _collect_decorators(source, match.start())
        functions.append(
            FunctionExport(
                name=name,
                params=_parse_params(params_str),
                return_type=return_type,
                docstring=None,
                decorators=decorators,
                is_async=is_async,
            )
        )

    for match in _CLASS_DECL_RE.finditer(source):
        name = match.group(1)
        if name.startswith("_"):
            continue
        bases: List[str] = []
        extends = match.group(2)
        if extends:
            bases.extend(b.strip() for b in extends.split(",") if b.strip())
        implements = match.group(3)
        is_abstract = "abstract class" in match.group(0)
        body = _extract_braced_body(source, match.end() - 1)
        methods = _extract_class_methods(body)
        classes.append(
            ClassExport(
                name=name,
                bases=bases,
                methods=methods,
                is_abstract=is_abstract or bool(implements),
            )
        )

    for match in _CONSTANT_RE.finditer(source):
        name = match.group(1)
        if name.startswith("_"):
            continue
        if name in arrow_names:
            continue
        if not _is_arrow_const(source, match.end() - 1):
            constants.append(name)

    for match in _IMPORT_RE.finditer(source):
        named = match.group(1)
        namespace = match.group(2)
        default = match.group(3)
        additional = match.group(4)
        module = match.group(5)
        parts: List[str] = []
        if default:
            parts.append(default.strip())
        if named:
            parts.extend(_clean_named_imports(named))
        if additional:
            parts.extend(_clean_named_imports(additional))
        if namespace:
            parts.append(namespace.strip())
        if parts:
            imports.append(f"import {{ {', '.join(parts)} }} from '{module}'")

    return ModuleExports(
        path=str(file_path),
        module_name=module_name,
        functions=functions,
        classes=classes,
        constants=constants,
        imports=imports,
    )


def _parse_params(params_str: str) -> List[ParamInfo]:
    params: List[ParamInfo] = []
    params_str = params_str.strip()
    if not params_str:
        return params
    for raw in _split_top_level(params_str):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("..."):
            params.append(ParamInfo(name=raw))
            continue
        if raw in ("this", "self"):
            continue
        name: Optional[str] = None
        type_annotation: Optional[str] = None
        default: Optional[str] = None
        if ":" in raw and not raw.startswith("{"):
            name_part, rest = raw.split(":", 1)
            name = _extract_param_name(name_part)
            if "=" in rest:
                type_part, default = rest.split("=", 1)
                type_annotation = type_part.strip() or None
                default = default.strip() or None
            else:
                type_annotation = rest.strip() or None
        else:
            if "=" in raw:
                name_part, default = raw.split("=", 1)
                name = _extract_param_name(name_part)
                default = default.strip() or None
            else:
                name = _extract_param_name(raw)
        if name and not name.startswith("_"):
            params.append(
                ParamInfo(
                    name=name,
                    type_annotation=type_annotation,
                    default=default,
                )
            )
    return params


def _extract_param_name(name_part: str) -> Optional[str]:
    name_part = name_part.strip()
    if not name_part:
        return None
    modifiers = {"public", "private", "protected", "readonly"}
    tokens = [t for t in name_part.split() if t not in modifiers]
    if not tokens:
        return None
    candidate = tokens[-1].rstrip(",")
    return candidate.strip("?") or None


def _split_top_level(params_str: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current = ""
    for ch in params_str:
        if ch in "([{<":
            depth += 1
            current += ch
        elif ch in ")]}>":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


def _clean_named_imports(blob: str) -> List[str]:
    return [n.strip() for n in blob.split(",") if n.strip()]


def _clean_type(type_str: Optional[str]) -> Optional[str]:
    if type_str is None:
        return None
    cleaned = type_str.strip().rstrip("{").strip()
    return cleaned or None


def _collect_decorators(source: str, decl_start: int) -> List[str]:
    decorators: List[str] = []
    lines = source[:decl_start].splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("@"):
            decorators.insert(0, stripped)
        else:
            break
    return decorators


def _extract_braced_body(source: str, open_brace_idx: int) -> str:
    """Return the body of a brace block, given the index of the opening brace."""
    if open_brace_idx >= len(source) or source[open_brace_idx] != "{":
        return ""
    depth = 0
    start = open_brace_idx + 1
    for i in range(open_brace_idx, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start:i]
    return ""


def _extract_class_methods(body: str) -> List[FunctionExport]:
    methods: List[FunctionExport] = []
    for m in _CLASS_METHOD_RE.finditer(body):
        name = m.group(1)
        if name in _SIMPLE_TYPE_KEYWORDS:
            continue
        if name.startswith("_") and name != "__init__":
            continue
        params_str = m.group(2) or ""
        return_type = _clean_type(m.group(3))
        is_async = "async " in m.group(0)
        methods.append(
            FunctionExport(
                name=name,
                params=_parse_params(params_str),
                return_type=return_type,
                docstring=None,
                decorators=[],
                is_async=is_async,
            )
        )
    return methods


def _is_arrow_const(source: str, after_eq_idx: int) -> bool:
    snippet = source[after_eq_idx : after_eq_idx + 200]
    return (
        bool(re.match(r"\s*(?:async\s+)?(?:\(|[A-Za-z_])", snippet))
        and "=>" in snippet[:200]
    )


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
        module_parts = parts[src_idx + 1 :]
    else:
        module_parts = parts

    if module_parts:
        last = module_parts[-1]
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            if last.endswith(ext):
                module_parts = module_parts[:-1] + (last[: -len(ext)],)
                break

    return ".".join(module_parts) if module_parts else file_path.stem
