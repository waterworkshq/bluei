from pathlib import Path
from unittest.mock import patch

from bluei.app.import_graph import (
    ImportGraph,
    build_import_graph,
    _resolve_python_module,
    _resolve_ts_module,
)


def test_empty_graph_topological_sort():
    graph = ImportGraph()
    assert graph.topological_sort() == []


def test_single_file_no_edges():
    graph = ImportGraph(files=["main.py"])
    result = graph.topological_sort()
    assert result == ["main.py"]


def test_topological_sort_respects_dependencies():
    graph = ImportGraph(files=["a.py", "b.py", "c.py"])
    graph.add_edge("a.py", "b.py")
    graph.add_edge("b.py", "c.py")
    result = graph.topological_sort()
    assert result.index("c.py") < result.index("b.py") < result.index("a.py")


def test_diamond_dependency():
    graph = ImportGraph(files=["a.py", "b.py", "c.py", "d.py"])
    graph.add_edge("a.py", "b.py")
    graph.add_edge("a.py", "c.py")
    graph.add_edge("b.py", "d.py")
    graph.add_edge("c.py", "d.py")
    result = graph.topological_sort()
    assert result.index("d.py") < result.index("b.py")
    assert result.index("d.py") < result.index("c.py")
    assert result.index("b.py") < result.index("a.py")
    assert result.index("c.py") < result.index("a.py")


def test_orphan_file_appended():
    graph = ImportGraph(files=["main.py", "utils.py"])
    graph.add_edge("main.py", "utils.py")
    result = graph.topological_sort()
    assert "utils.py" in result
    assert "main.py" in result
    assert result.index("utils.py") < result.index("main.py")


def test_build_import_graph_python(tmp_path):
    (tmp_path / "utils.py").write_text("def helper(): pass\n")
    (tmp_path / "main.py").write_text("from utils import helper\nhelper()\n")
    graph = build_import_graph(tmp_path, language="python")
    assert "main.py" in graph.files
    assert "utils.py" in graph.files
    assert len(graph.edges) == 1
    assert graph.edges[0].source == "main.py"
    assert graph.edges[0].target == "utils.py"


def test_build_import_graph_skips_node_modules(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.ts").write_text('import { x } from "./utils";\n')
    (src / "utils.ts").write_text("export const x = 1;\n")
    nm = tmp_path / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "index.ts").write_text("export const y = 2;\n")
    graph = build_import_graph(tmp_path, language="typescript")
    assert "node_modules/lib/index.ts" not in graph.files
    assert "src/app.ts" in graph.files


def test_build_import_graph_empty_dir(tmp_path):
    graph = build_import_graph(tmp_path, language="python")
    assert graph.files == []
    assert graph.edges == []


def test_build_import_graph_typescript(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "utils.ts").write_text(
        "export function add(a: number, b: number) { return a + b; }\n"
    )
    (src / "app.ts").write_text(
        'import { add } from "./utils";\nconsole.log(add(1, 2));\n'
    )
    graph = build_import_graph(tmp_path, language="typescript")
    has_edge = any(
        e.source == "src/app.ts" and e.target == "src/utils.ts" for e in graph.edges
    )
    assert has_edge, (
        f"Expected edge src/app.ts → src/utils.ts, got edges: {graph.edges}"
    )


# ── Extracted from test_app_small_modules_remaining.py ──
# Additional import graph edge cases


class TestImportGraphCycleHandling:
    def test_topological_sort_handles_cycle(self):
        graph = ImportGraph(files=["a.py", "b.py", "c.py"])
        graph.add_edge("a.py", "b.py")
        graph.add_edge("b.py", "a.py")
        result = graph.topological_sort()
        assert set(result) == {"a.py", "b.py", "c.py"}
        assert len(result) == 3


class TestResolvePythonModuleInit:
    def test_resolve_python_module_finds_init_file(self, tmp_path):
        pkg_dir = tmp_path / "mypackage"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        result = _resolve_python_module(tmp_path, "mypackage")
        assert result == "mypackage/__init__.py"

    def test_resolve_python_module_returns_none_for_external(self, tmp_path):
        result = _resolve_python_module(tmp_path, "sys")
        assert result is None

    def test_resolve_python_module_finds_py_file(self, tmp_path):
        (tmp_path / "utils.py").write_text("pass\n")
        result = _resolve_python_module(tmp_path, "utils")
        assert result == "utils.py"


class TestResolveTsModuleOutsideRepo:
    def test_resolve_ts_module_path_outside_repo(self, tmp_path):
        outside_file = tmp_path.parent / "outside_repo_file.ts"
        outside_file.write_text("// external")
        nested_src = tmp_path / "src" / "deep"
        nested_src.mkdir(parents=True)
        result = _resolve_ts_module(
            tmp_path, "../../../outside_repo_file", "src/deep/app.ts"
        )
        assert result is None

    def test_resolve_ts_module_returns_none_for_absolute(self, tmp_path):
        result = _resolve_ts_module(tmp_path, "react", "src/app.ts")
        assert result is None


class TestBuildImportGraphEdgeCases:
    def test_build_import_graph_oserror_on_file(self, tmp_path):
        (tmp_path / "bad.py").write_text("import os\n")
        with patch.object(Path, "read_text", side_effect=OSError("nope")):
            graph = build_import_graph(tmp_path, language="python")
        assert "bad.py" in graph.files

    def test_build_import_graph_skips_underscore_import(self, tmp_path):
        (tmp_path / "mod.py").write_text("import _private_helper\n")
        graph = build_import_graph(tmp_path, language="python")
        assert len(graph.edges) == 0

    def test_build_import_graph_javascript(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "utils.js").write_text("module.exports = { x: 1 };\n")
        (src / "app.js").write_text('const utils = require("./utils");\n')
        graph = build_import_graph(tmp_path, language="javascript")
        has_edge = any(
            e.source == "src/app.js" and e.target == "src/utils.js" for e in graph.edges
        )
        assert has_edge

    def test_build_import_graph_unknown_language_defaults_python(self, tmp_path):
        (tmp_path / "main.py").write_text("import os\n")
        graph = build_import_graph(tmp_path, language="unknown")
        assert "main.py" in graph.files
