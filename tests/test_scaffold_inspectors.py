"""Tests for scaffold.inspectors.typescript and scaffold.inspectors.go."""

from pathlib import Path

import pytest

from bluei.engine.scaffold.inspectors.go import (
    _first_identifier,
    _parse_go_params,
    _receiver_type_name,
    inspect_go_module,
)
from bluei.engine.scaffold.inspectors.typescript import (
    _clean_type,
    _parse_params,
    inspect_typescript_module,
)


def _write(td: Path, source: str, path: str = "src/mod.ts") -> Path:
    p = td / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


def _write_go(td: Path, source: str, path: str = "src/mod.go") -> Path:
    p = td / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


# ── TypeScript Inspector ─────────────────────────────────────────────────────


class TestTypeScriptFunctionExtraction:
    def test_simple_function(self, tmp_path):
        src = _write(
            tmp_path,
            "export function add(x: number, y: number): number { return x + y; }\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        assert len(ex.functions) == 1
        f = ex.functions[0]
        assert f.name == "add"
        assert f.return_type == "number"
        assert f.is_async is False
        assert len(f.params) == 2
        assert f.params[0].name == "x"
        assert f.params[0].type_annotation == "number"
        assert f.params[1].name == "y"

    def test_async_function(self, tmp_path):
        src = _write(
            tmp_path,
            "export async function fetch(url: string): Promise<Response> { return null; }\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        f = ex.functions[0]
        assert f.name == "fetch"
        assert f.is_async is True
        assert f.return_type == "Promise<Response>"

    def test_arrow_function(self, tmp_path):
        src = _write(
            tmp_path,
            "export const double = (x: number): number => x * 2;\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        f = ex.functions[0]
        assert f.name == "double"
        assert f.return_type == "number"
        assert f.is_async is False
        assert f.params[0].name == "x"

    def test_async_arrow_function(self, tmp_path):
        src = _write(
            tmp_path,
            "export const fetchAll = async (url: string): Promise<void> => {}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        f = ex.functions[0]
        assert f.name == "fetchAll"
        assert f.is_async is True
        assert f.return_type == "Promise<void>"

    def test_no_params_function(self, tmp_path):
        src = _write(tmp_path, "export function now(): number { return 0; }\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.functions[0].params == []

    def test_skips_private_functions(self, tmp_path):
        src = _write(
            tmp_path,
            "function _private(): void {}\nexport function public(): void {}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        names = [f.name for f in ex.functions]
        assert "public" in names
        assert "_private" not in names


class TestTypeScriptClassExtraction:
    def test_class_with_methods(self, tmp_path):
        src = _write(
            tmp_path,
            "export class Calculator {\n"
            "  add(a: number, b: number): number { return a + b; }\n"
            "  sub(a: number, b: number): number { return a - b; }\n"
            "}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        assert len(ex.classes) == 1
        cls = ex.classes[0]
        assert cls.name == "Calculator"
        method_names = [m.name for m in cls.methods]
        assert "add" in method_names
        assert "sub" in method_names
        assert cls.bases == []
        assert cls.is_abstract is False

    def test_class_with_extends(self, tmp_path):
        src = _write(
            tmp_path,
            "export class Scientific extends Calculator {}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.classes[0].bases == ["Calculator"]

    def test_class_with_implements_marks_abstract(self, tmp_path):
        src = _write(
            tmp_path,
            "export class Scientific implements ICompute {}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.classes[0].is_abstract is True

    def test_class_async_method(self, tmp_path):
        src = _write(
            tmp_path,
            "export class Api {\n"
            "  async fetch(url: string): Promise<Response> { return null; }\n"
            "}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        method = ex.classes[0].methods[0]
        assert method.is_async is True

    def test_skips_private_class(self, tmp_path):
        src = _write(
            tmp_path,
            "class _Private {}\nexport class Public {}\n",
        )
        ex = inspect_typescript_module(src, tmp_path)
        names = [c.name for c in ex.classes]
        assert "Public" in names
        assert "_Private" not in names


class TestTypeScriptConstantExtraction:
    def test_typed_constant(self, tmp_path):
        src = _write(tmp_path, "export const MAX: number = 100;\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert "MAX" in ex.constants

    def test_arrow_function_not_in_constants(self, tmp_path):
        src = _write(tmp_path, "export const double = (x: number) => x * 2;\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert "double" not in ex.constants
        assert any(f.name == "double" for f in ex.functions)

    def test_skips_private_constant(self, tmp_path):
        src = _write(
            tmp_path, "export const _INTERNAL = 1;\nexport const PUBLIC = 2;\n"
        )
        ex = inspect_typescript_module(src, tmp_path)
        assert "PUBLIC" in ex.constants
        assert "_INTERNAL" not in ex.constants


class TestTypeScriptImportExtraction:
    def test_named_import(self, tmp_path):
        src = _write(tmp_path, "import { Component } from '@angular/core';\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert len(ex.imports) == 1
        assert "Component" in ex.imports[0]
        assert "@angular/core" in ex.imports[0]

    def test_type_import(self, tmp_path):
        src = _write(tmp_path, "import type { Request } from 'express';\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert "Request" in ex.imports[0]
        assert "express" in ex.imports[0]

    def test_namespace_import(self, tmp_path):
        src = _write(tmp_path, "import * as utils from './utils';\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert "* as utils" in ex.imports[0]

    def test_multiple_named_imports(self, tmp_path):
        src = _write(tmp_path, "import { a, b, c } from 'lib';\n")
        ex = inspect_typescript_module(src, tmp_path)
        imp = ex.imports[0]
        assert "a" in imp and "b" in imp and "c" in imp


class TestTypeScriptModuleName:
    def test_module_name_strips_extension(self, tmp_path):
        src = _write(tmp_path, "export const x = 1;\n", path="src/utils.ts")
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.module_name == "utils"

    def test_module_name_with_dir(self, tmp_path):
        src = _write(tmp_path, "export const x = 1;\n", path="src/pkg/utils.ts")
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.module_name == "pkg.utils"


# ── Go Inspector ─────────────────────────────────────────────────────────────


class TestGoFunctionExtraction:
    def test_simple_function(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nfunc Add(a, b int) int { return a + b }\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert len(ex.functions) == 1
        f = ex.functions[0]
        assert f.name == "Add"
        assert f.return_type == "int"
        assert len(f.params) == 2

    def test_method_with_receiver(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\n"
            "type Calculator struct {}\n"
            "func (c *Calculator) Add(a, b int) int { return a + b }\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert all(f.name != "Add" for f in ex.functions)
        calc = next(c for c in ex.classes if c.name == "Calculator")
        method_names = [m.name for m in calc.methods]
        assert "Add" in method_names

    def test_method_with_value_receiver(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\n"
            "type Point struct {}\n"
            "func (p Point) Dist() float64 { return 0 }\n",
        )
        ex = inspect_go_module(src, tmp_path)
        point = next(c for c in ex.classes if c.name == "Point")
        method_names = [m.name for m in point.methods]
        assert "Dist" in method_names

    def test_skips_private_function(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nfunc _private() {}\nfunc Public() {}\n",
        )
        ex = inspect_go_module(src, tmp_path)
        names = [f.name for f in ex.functions]
        assert "Public" in names
        assert "_private" not in names

    def test_method_skips_if_no_type(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nfunc (c *Unknown) Do() {}\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert ex.functions == []
        assert ex.classes == []


class TestGoTypeExtraction:
    def test_struct_type(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\ntype Calculator struct {\n  result int\n}\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert len(ex.classes) == 1
        cls = ex.classes[0]
        assert cls.name == "Calculator"
        assert cls.bases == []
        assert cls.is_abstract is False

    def test_interface_type_is_abstract(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\ntype Stringer interface {\n  String() string\n}\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert ex.classes[0].is_abstract is True

    def test_type_alias(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\ntype MyInt = int\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert len(ex.classes) == 1
        assert ex.classes[0].name == "MyInt"
        assert "int" in ex.classes[0].bases

    def test_interface_methods_extracted(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\n"
            "type Stringer interface {\n"
            "  String() string\n"
            "  Format() string\n"
            "}\n",
        )
        ex = inspect_go_module(src, tmp_path)
        cls = ex.classes[0]
        names = [m.name for m in cls.methods]
        assert "String" in names
        assert "Format" in names

    def test_struct_methods_extracted(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\n"
            "type Calc struct {}\n"
            "func (c Calc) Add() int { return 0 }\n"
            "func (c Calc) Sub() int { return 0 }\n",
        )
        ex = inspect_go_module(src, tmp_path)
        cls = ex.classes[0]
        names = [m.name for m in cls.methods]
        assert "Add" in names
        assert "Sub" in names

    def test_skips_private_type(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\ntype _private struct {}\ntype Public struct {}\n",
        )
        ex = inspect_go_module(src, tmp_path)
        names = [c.name for c in ex.classes]
        assert "Public" in names
        assert "_private" not in names


class TestGoConstantExtraction:
    def test_simple_const(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nconst MaxValue = 100\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert "MaxValue" in ex.constants

    def test_const_block(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nconst (\n  Pi = 3.14\n  E  = 2.71\n)\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert "Pi" in ex.constants
        assert "E" in ex.constants

    def test_var(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nvar Global = 1\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert "Global" in ex.constants

    def test_var_block(self, tmp_path):
        src = _write_go(
            tmp_path,
            'package main\n\nvar (\n  Name = "x"\n  Age  = 0\n)\n',
        )
        ex = inspect_go_module(src, tmp_path)
        assert "Name" in ex.constants
        assert "Age" in ex.constants

    def test_skips_private_const(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nconst _internal = 1\nconst Public = 2\n",
        )
        ex = inspect_go_module(src, tmp_path)
        assert "Public" in ex.constants
        assert "_internal" not in ex.constants


class TestGoImportExtraction:
    def test_single_import(self, tmp_path):
        src = _write_go(
            tmp_path,
            'package main\n\nimport "fmt"\n',
        )
        ex = inspect_go_module(src, tmp_path)
        assert any('"fmt"' in imp for imp in ex.imports)

    def test_import_block(self, tmp_path):
        src = _write_go(
            tmp_path,
            'package main\n\nimport (\n  "fmt"\n  "os"\n)\n',
        )
        ex = inspect_go_module(src, tmp_path)
        assert any('"fmt"' in imp for imp in ex.imports)
        assert any('"os"' in imp for imp in ex.imports)

    def test_aliased_import(self, tmp_path):
        src = _write_go(
            tmp_path,
            'package main\n\nimport (\n  f "fmt"\n)\n',
        )
        ex = inspect_go_module(src, tmp_path)
        assert any('f "fmt"' in imp for imp in ex.imports)


class TestGoModuleName:
    def test_module_name_includes_package(self, tmp_path):
        src = _write_go(
            tmp_path,
            "package main\n\nfunc A() {}\n",
            path="src/cmd/main.go",
        )
        ex = inspect_go_module(src, tmp_path)
        assert ex.module_name == "cmd.main"

    def test_module_name_no_package_falls_back(self, tmp_path):
        src = tmp_path / "no_package.go"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("func A() {}\n", encoding="utf-8")
        ex = inspect_go_module(src, tmp_path)
        assert ex.module_name == "no_package"


# ── Edge cases and helpers ───────────────────────────────────────────────────


class TestEmptyModules:
    def test_empty_typescript(self, tmp_path):
        src = _write(tmp_path, "")
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.functions == []
        assert ex.classes == []
        assert ex.constants == []
        assert ex.imports == []

    def test_empty_go(self, tmp_path):
        src = _write_go(tmp_path, "package main\n")
        ex = inspect_go_module(src, tmp_path)
        assert ex.functions == []
        assert ex.classes == []
        assert ex.constants == []

    def test_typescript_comments_only(self, tmp_path):
        src = _write(tmp_path, "// just a comment\n/* block comment */\n")
        ex = inspect_typescript_module(src, tmp_path)
        assert ex.functions == []

    def test_go_comments_only(self, tmp_path):
        src = _write_go(tmp_path, "package main\n// comment\n")
        ex = inspect_go_module(src, tmp_path)
        assert ex.functions == []


class TestHelpers:
    def test_clean_type(self):
        assert _clean_type("number") == "number"
        assert _clean_type("number {") == "number"
        assert _clean_type("  string  ") == "string"
        assert _clean_type("") is None
        assert _clean_type(None) is None

    def test_parse_typescript_params(self):
        params = _parse_params("x: number, y: string = 'a'")
        assert len(params) == 2
        assert params[0].name == "x"
        assert params[0].type_annotation == "number"
        assert params[1].default == "'a'"

    def test_parse_typescript_optional_param(self):
        params = _parse_params("x?: number")
        assert params[0].name == "x"
        assert params[0].type_annotation == "number"

    def test_parse_typescript_rest_param(self):
        params = _parse_params("...args: any[]")
        assert any(p.name.startswith("...") for p in params)

    def test_parse_go_params(self):
        params = _parse_go_params("a, b int")
        assert len(params) == 2
        assert params[0].name == "a"
        assert params[1].name == "b"
        assert params[1].type_annotation == "int"

    def test_parse_go_params_typed_first(self):
        params = _parse_go_params("x int, y string")
        assert params[0].type_annotation == "int"
        assert params[1].type_annotation == "string"

    def test_receiver_type_name_pointer(self):
        assert _receiver_type_name("c *Calculator") == "Calculator"

    def test_receiver_type_name_value(self):
        assert _receiver_type_name("p Point") == "Point"

    def test_receiver_type_name_named(self):
        assert _receiver_type_name("c Calc") == "Calc"

    def test_first_identifier(self):
        assert _first_identifier("Pi = 3.14") == "Pi"
        assert _first_identifier('  Name = "x"') == "Name"
        assert _first_identifier("// comment") is None
        assert _first_identifier("") is None
