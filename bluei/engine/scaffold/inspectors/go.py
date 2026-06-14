"""scaffold.inspectors.go — Go module inspector (regex-based)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

_logger = logging.getLogger(__name__)

from ..models import ClassExport, FunctionExport, ModuleExports, ParamInfo


_FUNCTION_RE = re.compile(
    r"^func\s+(?:\(([^)]*)\)\s+)?(\w+)\s*\(([^)]*)\)\s*(?:\(([^)]*)\)|(\S+))?\s*\{",
    re.MULTILINE,
)

_TYPE_BODY_RE = re.compile(
    r"^type\s+(\w+)\s+(struct|interface)\s*\{",
    re.MULTILINE,
)

_TYPE_ALIAS_RE = re.compile(
    r"^type\s+(\w+)\s+=\s+(\S+)",
    re.MULTILINE,
)

_CONST_RE = re.compile(
    r"^const\s+(\w+)\s*(?:\w+\s*)?=",
    re.MULTILINE,
)

_CONST_BLOCK_RE = re.compile(
    r"^const\s*\((.*?)\n\)",
    re.DOTALL | re.MULTILINE,
)

_VAR_RE = re.compile(
    r"^var\s+(\w+)\s*(?:\w+\s*)?=",
    re.MULTILINE,
)

_VAR_BLOCK_RE = re.compile(
    r"^var\s*\((.*?)\n\)",
    re.DOTALL | re.MULTILINE,
)

_IMPORT_SINGLE_RE = re.compile(
    r'^\s*import\s+(?:\w+\s+)?"([^"]+)"',
    re.MULTILINE,
)

_IMPORT_BLOCK_RE = re.compile(
    r"^\s*import\s*\((.*?)\n\)",
    re.DOTALL | re.MULTILINE,
)

_IMPORT_LINE_RE = re.compile(
    r'(?:\s|^)(?:(\w+)\s+)?"([^"]+)"',
)

_PACKAGE_RE = re.compile(
    r"^package\s+(\w+)",
    re.MULTILINE,
)


def inspect_go_module(
    file_path: Path, worktree: Optional[Path] = None
) -> ModuleExports:
    """Inspect a Go source file and return its public exports.

    Uses regex-based extraction (tree-sitter is Phase 2).
    Maps Go types (struct, interface, alias) to ClassExport.
    Maps Go methods to ClassExport.methods.
    """
    source = file_path.read_text(encoding="utf-8")
    module_name = _infer_module_name(file_path, worktree)

    functions: List[FunctionExport] = []
    classes: List[ClassExport] = []
    constants: List[str] = []

    type_bodies: dict[str, str] = {}

    for match in _TYPE_BODY_RE.finditer(source):
        type_name = match.group(1)
        type_kind = match.group(2)
        body = _extract_braced_body(source, match.end() - 1)
        type_bodies[type_name] = body
        if type_name.startswith("_"):
            continue
        methods = _extract_type_methods(body)
        classes.append(
            ClassExport(
                name=type_name,
                bases=[],
                methods=methods,
                is_abstract=(type_kind == "interface"),
            )
        )

    for match in _TYPE_ALIAS_RE.finditer(source):
        name = match.group(1)
        target = match.group(2)
        if name.startswith("_"):
            continue
        classes.append(
            ClassExport(
                name=name,
                bases=[target],
                methods=[],
                is_abstract=False,
            )
        )

    type_to_class: dict[str, ClassExport] = {c.name: c for c in classes}
    existing_method_names: dict[str, set[str]] = {
        c.name: {m.name for m in c.methods} for c in classes
    }

    for match in _FUNCTION_RE.finditer(source):
        receiver = match.group(1)
        name = match.group(2)
        params_str = match.group(3) or ""
        return_type = _clean_go_return_type(match.group(4), match.group(5))

        if receiver:
            type_name = _receiver_type_name(receiver)
            if type_name and type_name in type_to_class:
                target = type_to_class[type_name]
                if (
                    not name.startswith("_")
                    and name not in existing_method_names[type_name]
                ):
                    target.methods.append(
                        FunctionExport(
                            name=name,
                            params=_parse_go_params(params_str),
                            return_type=return_type,
                            docstring=None,
                            decorators=[],
                            is_async=False,
                        )
                    )
                    existing_method_names[type_name].add(name)
            continue

        if name.startswith("_"):
            continue
        functions.append(
            FunctionExport(
                name=name,
                params=_parse_go_params(params_str),
                return_type=return_type,
                docstring=None,
                decorators=[],
                is_async=False,
            )
        )

    for match in _CONST_RE.finditer(source):
        name = match.group(1)
        if not name.startswith("_"):
            constants.append(name)

    for match in _CONST_BLOCK_RE.finditer(source):
        body = match.group(1)
        for line in body.splitlines():
            identifier = _first_identifier(line)
            if identifier and not identifier.startswith("_"):
                constants.append(identifier)

    for match in _VAR_RE.finditer(source):
        name = match.group(1)
        if not name.startswith("_"):
            constants.append(name)

    for match in _VAR_BLOCK_RE.finditer(source):
        body = match.group(1)
        for line in body.splitlines():
            identifier = _first_identifier(line)
            if identifier and not identifier.startswith("_"):
                constants.append(identifier)

    imports = _extract_imports(source)

    return ModuleExports(
        path=str(file_path),
        module_name=module_name,
        functions=functions,
        classes=classes,
        constants=constants,
        imports=imports,
    )


def _receiver_type_name(receiver: str) -> Optional[str]:
    parts = receiver.strip().split()
    if not parts:
        return None
    return parts[-1].lstrip("*")


def _parse_go_params(params_str: str) -> List[ParamInfo]:
    params: List[ParamInfo] = []
    params_str = params_str.strip()
    if not params_str:
        return params
    for raw in _split_top_level(params_str):
        raw = raw.strip()
        if not raw:
            continue
        if raw == "...":
            params.append(ParamInfo(name="..."))
            continue
        tokens = raw.split()
        if len(tokens) >= 2 and not tokens[0].startswith("..."):
            name = tokens[0]
            type_annotation = " ".join(tokens[1:])
            if name.startswith("..."):
                params.append(ParamInfo(name=name, type_annotation=type_annotation))
            else:
                params.append(ParamInfo(name=name, type_annotation=type_annotation))
        else:
            params.append(ParamInfo(name=raw))
    return params


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


def _clean_go_return_type(multi: Optional[str], single: Optional[str]) -> Optional[str]:
    if multi is not None:
        cleaned = multi.strip()
        return cleaned or None
    if single is not None:
        cleaned = single.strip()
        return cleaned or None
    return None


def _extract_type_methods(body: str) -> List[FunctionExport]:
    methods: List[FunctionExport] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.startswith("func "):
            m = re.match(
                r"^func\s+(?:\(([^)]*)\)\s+)?(\w+)\s*\(([^)]*)\)\s*"
                r"(?:\(([^)]*)\)|(\S+))?\s*$",
                stripped,
            )
            if not m:
                continue
            if m.group(1):
                continue
            name = m.group(2)
            params_str = m.group(3) or ""
            return_type = _clean_go_return_type(m.group(4), m.group(5))
        else:
            m = re.match(
                r"^(\w+)\s*\(([^)]*)\)\s*(?:\(([^)]*)\)|(\S+))?\s*$",
                stripped,
            )
            if not m:
                continue
            name = m.group(1)
            params_str = m.group(2) or ""
            return_type = _clean_go_return_type(m.group(3), m.group(4))
        if name.startswith("_"):
            continue
        methods.append(
            FunctionExport(
                name=name,
                params=_parse_go_params(params_str),
                return_type=return_type,
                docstring=None,
                decorators=[],
                is_async=False,
            )
        )
    return methods


def _first_identifier(line: str) -> Optional[str]:
    match = re.match(r"\s*(\w+)", line)
    return match.group(1) if match else None


def _extract_imports(source: str) -> List[str]:
    imports: List[str] = []
    for match in _IMPORT_SINGLE_RE.finditer(source):
        module = match.group(1)
        imports.append(f'import "{module}"')
    for match in _IMPORT_BLOCK_RE.finditer(source):
        body = match.group(1)
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            m = _IMPORT_LINE_RE.search(line)
            if m:
                alias = m.group(1)
                module = m.group(2)
                if alias:
                    imports.append(f'import {alias} "{module}"')
                else:
                    imports.append(f'import "{module}"')
    return imports


def _extract_braced_body(source: str, open_brace_idx: int) -> str:
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


def _infer_module_name(file_path: Path, worktree: Optional[Path] = None) -> str:
    package_name: Optional[str] = None
    try:
        source = file_path.read_text(encoding="utf-8")
        package_match = _PACKAGE_RE.search(source)
        if package_match:
            package_name = package_match.group(1)
    except (OSError, UnicodeDecodeError):
        _logger.debug("Failed to read source for package inference")

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

    if module_parts and module_parts[-1].endswith(".go"):
        module_parts = module_parts[:-1]

    path_part = ".".join(module_parts) if module_parts else file_path.stem
    if package_name:
        return f"{path_part}.{package_name}" if path_part else package_name
    return path_part
