"""Tests for scaffold.inspectors.python — Python module AST inspector."""

from pathlib import Path

from bluei.engine.scaffold.inspectors.python import (
    inspect_python_module,
    _infer_module_name,
)


class TestAnnotatedAssignment:
    def test_annotated_assignment(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("x: int = 1\n", encoding="utf-8")
        exports = inspect_python_module(src, tmp_path)
        assert "x" in exports.constants

    def test_private_annotated_assignment_skipped(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("_x: int = 1\n", encoding="utf-8")
        exports = inspect_python_module(src, tmp_path)
        assert "_x" not in exports.constants


class TestFunctionParamEdgeCases:
    def test_vararg_and_kwarg(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("def func(*args, **kwargs): pass\n", encoding="utf-8")
        exports = inspect_python_module(src, tmp_path)
        func = exports.functions[0]
        names = [p.name for p in func.params]
        assert "*args" in names
        assert "**kwargs" in names

    def test_kwonly_args(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("def func(*, key: str, val: int = 0): pass\n", encoding="utf-8")
        exports = inspect_python_module(src, tmp_path)
        func = exports.functions[0]
        names = [p.name for p in func.params]
        assert "key" in names
        assert "val" in names

    def test_self_and_cls_params_skipped(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text(
            "class Foo:\n    def method(self, x): pass\n\n"
            "class Bar:\n    @classmethod\n    def cm(cls, y): pass\n",
            encoding="utf-8",
        )
        exports = inspect_python_module(src, tmp_path)
        for cls in exports.classes:
            for method in cls.methods:
                param_names = [p.name for p in method.params]
                assert "self" not in param_names
                assert "cls" not in param_names


class TestFunctionDocstringAndDecorators:
    def test_function_with_docstring_and_decorators(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text(
            '@my_decorator\ndef greet():\n    """Hello."""\n    pass\n',
            encoding="utf-8",
        )
        exports = inspect_python_module(src, tmp_path)
        func = exports.functions[0]
        assert func.name == "greet"
        assert func.docstring == "Hello."
        assert len(func.decorators) == 1


class TestClassAbstractDetection:
    def test_class_with_abc_decorator(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text(
            "import abc\n\n@abc.ABC\nclass Base:\n    pass\n", encoding="utf-8"
        )
        exports = inspect_python_module(src, tmp_path)
        assert exports.classes[0].is_abstract is True

    def test_class_with_abcmeta_base(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text(
            "import abc\n\nclass Base(abc.ABCMeta):\n    pass\n", encoding="utf-8"
        )
        exports = inspect_python_module(src, tmp_path)
        assert exports.classes[0].is_abstract is True

    def test_class_with_abc_base(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("import abc\n\nclass Base(ABC):\n    pass\n", encoding="utf-8")
        exports = inspect_python_module(src, tmp_path)
        assert exports.classes[0].is_abstract is True

    def test_class_skips_private_methods_except_init(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text(
            "class Foo:\n    def __init__(self): pass\n    def _private(self): pass\n    def public(self): pass\n",
            encoding="utf-8",
        )
        exports = inspect_python_module(src, tmp_path)
        methods = exports.classes[0].methods
        method_names = [m.name for m in methods]
        assert "__init__" in method_names
        assert "public" in method_names
        assert "_private" not in method_names


class TestImportExtraction:
    def test_import_extraction(self, tmp_path):
        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("import os\nfrom pathlib import Path\n", encoding="utf-8")
        exports = inspect_python_module(src, tmp_path)
        assert len(exports.imports) == 2


class TestInferModuleName:
    def test_infer_module_name_no_worktree(self, tmp_path):
        src = tmp_path / "some" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("x = 1\n", encoding="utf-8")
        result = _infer_module_name(src, None)
        assert "mod" in result

    def test_infer_module_name_outside_worktree(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("x = 1\n", encoding="utf-8")
        other = tmp_path / "other"
        other.mkdir()
        result = _infer_module_name(src, other)
        assert "mod" in result

    def test_infer_module_name_no_src(self, tmp_path):
        src = tmp_path / "pkg" / "mod.py"
        src.parent.mkdir(parents=True)
        src.write_text("x = 1\n", encoding="utf-8")
        result = _infer_module_name(src, tmp_path)
        assert "pkg.mod" == result

    def test_infer_module_name_init_file(self, tmp_path):
        src = tmp_path / "src" / "pkg" / "__init__.py"
        src.parent.mkdir(parents=True)
        src.write_text("", encoding="utf-8")
        result = _infer_module_name(src, tmp_path)
        assert "pkg" == result
