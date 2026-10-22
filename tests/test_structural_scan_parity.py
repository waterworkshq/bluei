"""Parity: the three structural-subtree walkers must stay behaviorally identical.

``_iter_structural_subtrees`` exists in three places by design:

1. the canonical shadow-scan walker (``bluei.app.emergent_rules``),
2. the committed graduated pack (``plugins/graduated-eslint/plugin.py``),
3. the graduator's plugin template (``bluei/tools/graduator/templates.py``).

The pack re-implements the walk inline so it stays self-contained, and the
template generates future packs — divergence means graduated packs scan
differently from shadow scans. These tests pin the parity.

Note on the template site: its walker lives inside the ``PLUGIN_TEMPLATE``
string, so "importing" it means rendering a real structural plugin via
``render_plugin_py`` and importing that (same importlib pattern
``tests/test_graduator.py`` uses for generated packs).
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

from bluei.app.emergent_rules import _iter_structural_subtrees as _walk_emergent
from bluei.tools.graduator.templates import render_plugin_py

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Same simple module for every site: module-level containers + inner
# statements (a return and a raise) that the walker must emit.
_SAMPLE = (
    "def f():\n"
    "    return 1\n"
    "\n"
    "class C:\n"
    "    def m(self):\n"
    "        raise ValueError('x')\n"
)


def _import_from_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_committed_pack():
    return _import_from_file(
        "graduated_eslint_pack_parity",
        _REPO_ROOT / "plugins" / "graduated-eslint" / "plugin.py",
    )


def _render_template_pack(tmp_path: Path):
    src = render_plugin_py(
        pack_id="graduated-parity",
        rule_id="er-parity",
        rule_header="Parity probe",
        language="python",
        category="lint",
        confidence=0.9,
        detection_type="structural",
        search_pattern="",
        file_glob="**/*.py",
        ast_blob=json.dumps({"hash": "0"}),
        evidence_count=1,
    )
    path = tmp_path / "plugin.py"
    path.write_text(src, encoding="utf-8")
    return _import_from_file("graduated_template_pack_parity", path)


def _nested_function_tree(depth: int) -> ast.Module:
    """Build ``depth``-deep nested FunctionDefs WITHOUT source text."""

    def fn(name: str, body: list) -> ast.FunctionDef:
        return ast.FunctionDef(
            name=name,
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=body,
            decorator_list=[],
        )

    node = fn("f0", [ast.Expr(value=ast.Constant(value="leaf"))])
    for i in range(1, depth):
        node = fn(f"f{i}", [node])
    return ast.Module(body=[node], type_ignores=[])


def test_structural_subtree_parity_on_simple_module(tmp_path):
    tree = ast.parse(_SAMPLE)
    committed = _load_committed_pack()
    rendered = _render_template_pack(tmp_path)

    dumps = [
        [ast.dump(s) for s in walker(tree)]
        for walker in (
            _walk_emergent,
            committed._iter_structural_subtrees,
            rendered._iter_structural_subtrees,
        )
    ]
    assert dumps[0] == dumps[1]
    assert dumps[1] == dumps[2]
    # Sanity: the sample actually yields the two inner statements.
    assert len(dumps[0]) == 2


def test_structural_depth_cap_parity_on_deep_constructed_tree(tmp_path):
    # 200 nesting levels >> the 128 cap: all three walkers must truncate
    # identically (the 200-deep leaf statement is never reached).
    tree = _nested_function_tree(200)
    committed = _load_committed_pack()
    rendered = _render_template_pack(tmp_path)

    results = [
        walker(tree)
        for walker in (
            _walk_emergent,
            committed._iter_structural_subtrees,
            rendered._iter_structural_subtrees,
        )
    ]
    assert all(isinstance(r, list) for r in results)
    assert results[0] == results[1] == results[2] == []
