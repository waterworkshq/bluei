"""scaffold.models — Data models for module introspection and scaffold results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class ParamInfo:
    name: str
    type_annotation: Optional[str] = None
    default: Optional[str] = None


@dataclass
class FunctionExport:
    name: str
    params: List[ParamInfo] = field(default_factory=list)
    return_type: Optional[str] = None
    docstring: Optional[str] = None
    decorators: List[str] = field(default_factory=list)
    is_async: bool = False


@dataclass
class ClassExport:
    name: str
    bases: List[str] = field(default_factory=list)
    methods: List[FunctionExport] = field(default_factory=list)
    is_abstract: bool = False


@dataclass
class ModuleExports:
    path: str
    module_name: str
    functions: List[FunctionExport] = field(default_factory=list)
    classes: List[ClassExport] = field(default_factory=list)
    constants: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)


@dataclass
class ScaffoldResult:
    success: bool
    output_path: Optional[Path] = None
    content: str = ""
    error: Optional[str] = None
