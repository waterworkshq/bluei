"""Tests for scaffold package — module introspection, test generation, doc scaffolding."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.scaffold.engine import ScaffoldEngine, _insert_section
from bluei.engine.scaffold.framework_detect import (
    detect_install_commands,
    detect_test_commands,
    detect_test_framework,
)
from bluei.engine.scaffold.inspectors.python import inspect_python_module
from bluei.engine.scaffold.models import (
    ClassExport,
    FunctionExport,
    ModuleExports,
    ParamInfo,
    ScaffoldResult as ScaffoldRes,
)
from bluei.engine.scaffold.path_resolver import (
    TestPathResolver as PathResolver,
)
from bluei.engine.scaffold.templates import (
    render_quickstart_section,
    render_rollback_section,
    render_test_file,
)
from bluei.engine.scaffold.doc_scaffolder import (
    generate_quickstart_content,
    generate_rollback_content,
    insert_section,
)


def _write_module(td: Path, source: str, path: str = "src/pkg/mod.py") -> Path:
    p = td / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return p


# ── Python Inspector ────────────────────────────────────────────────────────


class TestPythonInspector:
    def test_extracts_public_functions(self, tmp_path):
        src = _write_module(tmp_path, "def hello(): pass\ndef _private(): pass\n")
        exports = inspect_python_module(src, tmp_path)
        assert len(exports.functions) == 1
        assert exports.functions[0].name == "hello"

    def test_extracts_async_function(self, tmp_path):
        src = _write_module(tmp_path, "async def fetch(): pass\n")
        exports = inspect_python_module(src, tmp_path)
        assert exports.functions[0].is_async

    def test_extracts_function_params(self, tmp_path):
        src = _write_module(
            tmp_path, "def greet(name: str, age: int = 0) -> str: pass\n"
        )
        exports = inspect_python_module(src, tmp_path)
        func = exports.functions[0]
        assert len(func.params) == 2
        assert func.params[0].name == "name"
        assert func.params[0].type_annotation == "str"
        assert func.params[1].default == "0"

    def test_extracts_return_type(self, tmp_path):
        src = _write_module(tmp_path, "def get() -> dict: pass\n")
        exports = inspect_python_module(tmp_path / "src" / "pkg" / "mod.py", tmp_path)
        assert exports.functions[0].return_type == "dict"

    def test_extracts_classes(self, tmp_path):
        src = _write_module(
            tmp_path, "class Foo:\n    def bar(self): pass\n    def quux(self): pass\n"
        )
        exports = inspect_python_module(src, tmp_path)
        assert len(exports.classes) == 1
        assert exports.classes[0].name == "Foo"
        assert len(exports.classes[0].methods) == 2

    def test_skips_private_classes(self, tmp_path):
        src = _write_module(tmp_path, "class _Private: pass\nclass Public: pass\n")
        exports = inspect_python_module(src, tmp_path)
        assert len(exports.classes) == 1
        assert exports.classes[0].name == "Public"

    def test_extracts_constants(self, tmp_path):
        src = _write_module(tmp_path, "PUBLIC = 1\n_INTERNAL = 2\n")
        exports = inspect_python_module(src, tmp_path)
        assert "PUBLIC" in exports.constants
        assert "_INTERNAL" not in exports.constants

    def test_infers_module_name(self, tmp_path):
        src = _write_module(tmp_path, "x = 1\n")
        exports = inspect_python_module(src, tmp_path)
        assert exports.module_name == "pkg.mod"

    def test_handles_empty_module(self, tmp_path):
        src = _write_module(tmp_path, "")
        exports = inspect_python_module(src, tmp_path)
        assert exports.functions == []
        assert exports.classes == []

    def test_handles_syntax_error(self, tmp_path):
        src = _write_module(tmp_path, "def foo(:\n")
        with pytest.raises(SyntaxError):
            inspect_python_module(src, tmp_path)

    def test_extracts_init_method(self, tmp_path):
        src = _write_module(tmp_path, "class Foo:\n    def __init__(self, x): pass\n")
        exports = inspect_python_module(src, tmp_path)
        assert any(m.name == "__init__" for m in exports.classes[0].methods)


# ── Test Path Resolver ──────────────────────────────────────────────────────


class TestPathResolverTests:
    def test_src_subdir_to_test(self, tmp_path):
        r = PathResolver()
        result = r.source_to_test("src/myapp/utils.py", tmp_path)
        assert result == tmp_path / "tests" / "test_utils.py"

    def test_src_top_level_to_test(self, tmp_path):
        r = PathResolver()
        result = r.source_to_test("src/main.py", tmp_path)
        assert result == tmp_path / "tests" / "test_main.py"

    def test_plain_to_test(self, tmp_path):
        r = PathResolver()
        result = r.source_to_test("helper.py", tmp_path)
        assert result == tmp_path / "tests" / "test_helper.py"

    def test_resolve_test_to_source(self, tmp_path):
        r = PathResolver()
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("x = 1", encoding="utf-8")
        result = r.test_to_source("tests/test_app.py", tmp_path)
        assert result == tmp_path / "src" / "app.py"

    def test_resolve_test_to_source_not_found(self, tmp_path):
        r = PathResolver()
        result = r.test_to_source("tests/test_missing.py", tmp_path)
        assert result is None

    def test_infer_module_name_from_src(self, tmp_path):
        r = PathResolver()
        name = r.infer_module_name("src/pkg/utils.py", tmp_path)
        assert name == "pkg.utils"

    def test_infer_module_name_plain(self, tmp_path):
        r = PathResolver()
        name = r.infer_module_name("utils.py", tmp_path)
        assert name == "utils"


# ── Framework Detection ─────────────────────────────────────────────────────


class TestFrameworkDetect:
    def test_pytest_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_conftest(self, tmp_path):
        (tmp_path / "conftest.py").write_text("import pytest\n", encoding="utf-8")
        assert detect_test_framework(tmp_path) == "pytest"

    def test_default_is_pytest(self, tmp_path):
        assert detect_test_framework(tmp_path) == "pytest"

    def test_jest_from_package_json(self, tmp_path):
        pkg = {"devDependencies": {"jest": "^29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        assert detect_test_framework(tmp_path, "javascript") == "jest"

    def test_vitest_from_package_json(self, tmp_path):
        pkg = {"devDependencies": {"vitest": "^0.34.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        assert detect_test_framework(tmp_path, "javascript") == "vitest"

    def test_detect_install_commands(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("pytest", encoding="utf-8")
        result = detect_install_commands(tmp_path)
        assert "pytest" in result

    def test_detect_test_commands(self, tmp_path):
        result = detect_test_commands(tmp_path)
        assert "pytest" in result


# ── Templates ───────────────────────────────────────────────────────────────


class TestTemplates:
    def test_render_test_file_with_functions(self):
        exports = ModuleExports(
            path="src/app.py",
            module_name="app",
            functions=[FunctionExport(name="hello")],
        )
        content = render_test_file(exports)
        assert "from app import hello" in content
        assert "def test_hello_basic" in content
        compile(content, "test_app.py", "exec")

    def test_render_test_file_with_classes(self):
        exports = ModuleExports(
            path="src/app.py",
            module_name="app",
            classes=[ClassExport(name="Foo", methods=[FunctionExport(name="bar")])],
        )
        content = render_test_file(exports)
        assert "class TestFoo" in content
        assert "def test_bar_basic" in content
        compile(content, "test_app.py", "exec")

    def test_render_empty_module(self):
        exports = ModuleExports(path="src/app.py", module_name="app")
        content = render_test_file(exports)
        assert "no public exports" in content

    def test_render_rollback_section(self):
        content = render_rollback_section("pytest -q")
        assert "## Rollback" in content
        assert "pytest -q" in content

    def test_render_quickstart_section(self):
        content = render_quickstart_section("pip install pytest", "pytest -q")
        assert "## Quick start" in content
        assert "pip install pytest" in content


# ── Scaffold Engine ─────────────────────────────────────────────────────────


class TestScaffoldEngine:
    def test_generate_test_file_end_to_end(self, tmp_path):
        src = tmp_path / "src" / "myapp" / "utils.py"
        src.parent.mkdir(parents=True)
        src.write_text("def add(a, b): return a + b\n", encoding="utf-8")

        engine = ScaffoldEngine()
        test_path = tmp_path / "tests" / "test_utils.py"
        result = engine.generate_test_file(test_path, tmp_path)
        assert result.success
        assert result.output_path.exists()
        content = result.output_path.read_text(encoding="utf-8")
        assert "test_add_basic" in content
        compile(content, str(test_path), "exec")

    def test_generate_test_file_source_not_found(self, tmp_path):
        engine = ScaffoldEngine()
        test_path = tmp_path / "tests" / "test_missing.py"
        result = engine.generate_test_file(test_path, tmp_path)
        assert not result.success

    def test_generate_test_file_already_exists(self, tmp_path):
        src = tmp_path / "src" / "myapp" / "utils.py"
        src.parent.mkdir(parents=True)
        src.write_text("def add(a, b): return a + b\n", encoding="utf-8")
        test_path = tmp_path / "tests" / "test_utils.py"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("pass\n", encoding="utf-8")

        engine = ScaffoldEngine()
        result = engine.generate_test_file(test_path, tmp_path)
        assert not result.success
        assert "already exists" in result.error

    def test_generate_doc_rollback_section(self, tmp_path):
        doc = tmp_path / "docs" / "ops.md"
        doc.parent.mkdir(parents=True)
        doc.write_text(
            "# Operations\n\n## Deploy\nDeploy steps here.\n", encoding="utf-8"
        )

        engine = ScaffoldEngine()
        result = engine.generate_doc_section(doc, "rollback", worktree=tmp_path)
        assert result.success
        assert "## Rollback" in doc.read_text(encoding="utf-8")

    def test_generate_doc_duplicate_section_skipped(self, tmp_path):
        doc = tmp_path / "docs" / "ops.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# Ops\n\n## Rollback\nExisting.\n", encoding="utf-8")

        engine = ScaffoldEngine()
        result = engine.generate_doc_section(doc, "rollback", worktree=tmp_path)
        assert not result.success
        assert "already exists" in result.error


# ── Doc Scaffolder ──────────────────────────────────────────────────────────


class TestDocScaffolder:
    def test_insert_section_at_end(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n\nSome content.\n", encoding="utf-8")
        ok = insert_section(doc, "## Notes", "## Notes\nSome notes.\n")
        assert ok
        text = doc.read_text(encoding="utf-8")
        assert "## Notes" in text

    def test_insert_section_before_heading(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n\n## Existing\nContent.\n", encoding="utf-8")
        ok = insert_section(
            doc, "## New", "## New\nNew content.\n", before_heading="## Existing"
        )
        assert ok
        text = doc.read_text(encoding="utf-8")
        assert text.index("## New") < text.index("## Existing")

    def test_duplicate_section_not_inserted(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n\n## Notes\nExisting.\n", encoding="utf-8")
        ok = insert_section(doc, "## Notes", "## Notes\nNew.\n")
        assert not ok

    def test_missing_file_returns_false(self, tmp_path):
        ok = insert_section(tmp_path / "missing.md", "## Notes", "## Notes\n")
        assert not ok

    def test_generate_rollback_content(self, tmp_path):
        content = generate_rollback_content(tmp_path)
        assert "## Rollback" in content

    def test_generate_quickstart_content(self, tmp_path):
        content = generate_quickstart_content(tmp_path)
        assert "## Quick start" in content


# ── Insert Section Helper ───────────────────────────────────────────────────


class TestInsertSection:
    def test_insert_at_end(self):
        result = _insert_section("# Title\n", "## New Section\nContent.\n")
        assert "## New Section" in result

    def test_insert_before(self):
        result = _insert_section(
            "# Title\n\n## After\n", "## New\n", insert_before="## After"
        )
        assert result.index("## New") < result.index("## After")

    def test_insert_after(self):
        result = _insert_section(
            "# Title\n\n## Before\n", "## New\n", insert_after="## Before"
        )
        assert "## New" in result


# ── Models ──────────────────────────────────────────────────────────────────


class TestModels:
    def test_param_info_defaults(self):
        p = ParamInfo(name="x")
        assert p.type_annotation is None
        assert p.default is None

    def test_function_export_defaults(self):
        f = FunctionExport(name="foo")
        assert f.params == []
        assert f.docstring is None
        assert f.is_async is False

    def test_scaffold_result_success(self):
        r = ScaffoldRes(success=True, content="hello")
        assert r.success
        assert r.error is None

    def test_scaffold_result_failure(self):
        r = ScaffoldRes(success=False, error="broken")
        assert not r.success


# ── Tests extracted from test_campaigns_scaffold_tiers_remaining.py ─────────


class TestScaffoldEngineUnsupportedLanguage:
    def test_unsupported_language_returns_error(self, tmp_path):
        src = tmp_path / "src" / "app.py"
        src.parent.mkdir(parents=True)
        src.write_text("def foo(): pass\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_test_file(
            tmp_path / "tests" / "test_app.py", tmp_path, language="rust"
        )
        assert not result.success
        assert "unsupported language" in result.error


class TestScaffoldEnginePythonErrors:
    def test_syntax_error_in_source(self, tmp_path):
        src = tmp_path / "src" / "broken.py"
        src.parent.mkdir(parents=True)
        src.write_text("def foo(:\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_test_file(
            tmp_path / "tests" / "test_broken.py", tmp_path
        )
        assert not result.success
        assert "syntax error" in result.error.lower()

    def test_inspection_exception(self, tmp_path):
        src = tmp_path / "src" / "app.py"
        src.parent.mkdir(parents=True)
        src.write_text("x = 1\n", encoding="utf-8")
        engine = ScaffoldEngine()
        with patch(
            "bluei.engine.scaffold.engine.inspect_python_module",
            side_effect=OSError("nope"),
        ):
            result = engine.generate_test_file(
                tmp_path / "tests" / "test_app.py", tmp_path
            )
        assert not result.success
        assert "inspection failed" in result.error

    def test_no_public_exports(self, tmp_path):
        src = tmp_path / "src" / "empty.py"
        src.parent.mkdir(parents=True)
        src.write_text("_private = 1\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_test_file(
            tmp_path / "tests" / "test_empty.py", tmp_path
        )
        assert not result.success
        assert "no public exports" in result.error

    def test_generated_code_syntax_error(self, tmp_path):
        src = tmp_path / "src" / "app.py"
        src.parent.mkdir(parents=True)
        src.write_text("def foo(): pass\n", encoding="utf-8")
        engine = ScaffoldEngine()
        bad_content = "def broken(:\n"
        with patch(
            "bluei.engine.scaffold.engine.render_test_file", return_value=bad_content
        ):
            result = engine.generate_test_file(
                tmp_path / "tests" / "test_app.py", tmp_path
            )
        assert not result.success
        assert "syntax error" in result.error.lower()


class TestScaffoldEngineDocSections:
    def test_doc_section_file_not_found(self, tmp_path):
        engine = ScaffoldEngine()
        result = engine.generate_doc_section(tmp_path / "missing.md", "rollback")
        assert not result.success
        assert "not found" in result.error

    def test_unknown_section_type(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_doc_section(doc, "custom_section", worktree=tmp_path)
        assert not result.success
        assert "unknown section type" in result.error

    def test_quickstart_section_insertion(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n\n## Something\nContent.\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_doc_section(doc, "quickstart", worktree=tmp_path)
        assert result.success
        assert "## Quick start" in doc.read_text(encoding="utf-8")

    def test_rollback_section_no_worktree(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_doc_section(doc, "rollback", worktree=None)
        assert result.success
        assert "## Rollback" in doc.read_text(encoding="utf-8")

    def test_quickstart_section_no_worktree(self, tmp_path):
        doc = tmp_path / "doc.md"
        doc.write_text("# Title\n", encoding="utf-8")
        engine = ScaffoldEngine()
        result = engine.generate_doc_section(doc, "quickstart", worktree=None)
        assert result.success
        assert "## Quick start" in doc.read_text(encoding="utf-8")


class TestInsertSectionEdgeCases:
    def test_insert_before_not_found_appends(self):
        result = _insert_section("# Title\n", "## New\n", insert_before="## Missing")
        assert "## New" in result

    def test_insert_after_not_found_appends(self):
        result = _insert_section("# Title\n", "## New\n", insert_after="## Missing")
        assert "## New" in result

    def test_insert_after_with_no_newline_prefix(self):
        doc = "# Title\n\n## Before Content"
        result = _insert_section(doc, "## New\n", insert_after="## Before")
        assert "## New" in result
        assert "## Before" in result


class TestScaffoldEngineTestPathFallback:
    def test_test_path_not_resolved_falls_back(self, tmp_path):
        src = tmp_path / "src" / "my_module.py"
        src.parent.mkdir(parents=True)
        src.write_text("def hello(): pass\n", encoding="utf-8")
        engine = ScaffoldEngine()
        test_path = tmp_path / "tests" / "test_my_module.py"
        result = engine.generate_test_file(test_path, tmp_path)
        assert result.success

    def test_test_path_non_py_extension(self, tmp_path):
        src = tmp_path / "src" / "app.py"
        src.parent.mkdir(parents=True)
        src.write_text("def hello(): pass\n", encoding="utf-8")
        engine = ScaffoldEngine()
        test_path = tmp_path / "tests" / "test_app.txt"
        result = engine.generate_test_file(test_path, tmp_path)
        assert not result.success
