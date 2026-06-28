"""Python-specific structural normalization via AST traversal.

The four `_python_*` dispatcher functions parse the AST, then delegate
to the text-based normalizers in `.text` for the final pass. Imports
from `.text` are deferred to avoid any chance of circular import at
package init time.
"""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Dict, List


def _python_structural_hash(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        from bluei.engine.structural_hash.text import _text_structural_hash

        return _text_structural_hash(code)
    normalizer = _PythonStructuralNormalizer()
    normalizer.visit(tree)
    return hashlib.sha256(" ".join(normalizer.tokens).encode("utf-8")).hexdigest()[:16]


def _python_node_sequence(code: str) -> List[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        from bluei.engine.structural_hash.text import _text_node_sequence

        return _text_node_sequence(code)
    visitor = _NodeSequenceExtractor()
    visitor.visit(tree)
    return visitor.sequence


def _python_operator_sequence(code: str) -> List[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        from bluei.engine.structural_hash.text import _text_operator_sequence

        return _text_operator_sequence(code)
    extractor = _OperatorExtractor()
    extractor.visit(tree)
    return extractor.operators


def _python_normalize_for_sharing(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        from bluei.engine.structural_hash.text import _text_normalize_for_sharing

        return _text_normalize_for_sharing(code)
    normalizer = _SharingNormalizer()
    normalizer.visit(tree)
    return ast.unparse(tree)


def extract_imports_touched(code: str, language: str = "python") -> List[str]:
    """Extract actual module names from import statements.

    Unlike _PythonStructuralNormalizer.visit_Import which tokenizes as <mod>,
    this preserves real module names for the imports_touched envelope field.

    Returns sorted list of unique module names. Empty list on SyntaxError
    or unsupported language.

    Supports Python (via AST) and TypeScript/JavaScript (via regex):
    ESM `import`/`from`, side-effect imports, and CommonJS `require()`.
    """
    if language in ("typescript", "javascript", "tsx", "jsx"):
        return _extract_ts_imports(code)
    if language != "python":
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    extractor = _ImportNameExtractor()
    extractor.visit(tree)
    return sorted(set(extractor.modules))


# Matches:
#   import x from 'mod'
#   import { a } from "mod"
#   import * as ns from 'mod'
#   import 'mod'                          (side-effect)
#   const x = require('mod')              (CommonJS)
#   const { a } = require("mod")          (CommonJS destructure)
_TS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[^'"]+\s+from\s+)?|const\s+[^=]+=\s*require\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _extract_ts_imports(code: str) -> List[str]:
    """Extract TypeScript/JavaScript module names via regex (no AST dependency)."""
    return sorted(set(_TS_IMPORT_RE.findall(code)))


class _ImportNameExtractor(ast.NodeVisitor):
    def __init__(self):
        self.modules: List[str] = []

    def visit_Import(self, node):
        for alias in node.names:
            self.modules.append(alias.name)

    def visit_ImportFrom(self, node):
        if node.module:
            self.modules.append(node.module)


class _PythonStructuralNormalizer(ast.NodeVisitor):
    def __init__(self):
        self.tokens: List[str] = []
        self._var_map: Dict[str, str] = {}
        self._var_counter: int = 0

    def _map_var(self, name: str) -> str:
        if name in self._var_map:
            return self._var_map[name]
        mapped = f"_v{self._var_counter}"
        self._var_counter += 1
        self._var_map[name] = mapped
        return mapped

    def visit_Module(self, node):
        for child in node.body:
            self.visit(child)

    def visit_Expr(self, node):
        self.visit(node.value)

    def visit_Assign(self, node):
        for target in node.targets:
            self.visit(target)
        self.tokens.append("=")
        self.visit(node.value)

    def visit_AugAssign(self, node):
        self.visit(node.target)
        self.tokens.append(self._op_str(node.op))
        self.tokens.append("=")
        self.visit(node.value)

    def visit_AnnAssign(self, node):
        if node.target:
            self.visit(node.target)
        if node.annotation:
            self.tokens.append(":")
            self.visit(node.annotation)
        if node.value:
            self.tokens.append("=")
            self.visit(node.value)

    def visit_BinOp(self, node):
        self.visit(node.left)
        self.tokens.append(self._op_str(node.op))
        self.visit(node.right)

    def visit_UnaryOp(self, node):
        self.tokens.append(self._op_str(node.op))
        self.visit(node.operand)

    def visit_Compare(self, node):
        self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            self.tokens.append(self._cmpop_str(op))
            self.visit(comparator)

    def visit_BoolOp(self, node):
        for i, value in enumerate(node.values):
            self.visit(value)
            if i < len(node.values) - 1:
                self.tokens.append(self._boolop_str(node.op))

    def visit_Call(self, node):
        self.visit(node.func)
        self.tokens.append("(")
        for i, arg in enumerate(node.args):
            if i > 0:
                self.tokens.append(",")
            self.visit(arg)
        for i, kw in enumerate(node.keywords):
            if node.args or i > 0:
                self.tokens.append(",")
            self.tokens.append(self._map_var(kw.arg) if kw.arg else "**")
            self.visit(kw.value)
        self.tokens.append(")")

    def visit_Attribute(self, node):
        self.visit(node.value)
        self.tokens.append(".")
        self.tokens.append(node.attr)

    def visit_Name(self, node):
        self.tokens.append(self._map_var(node.id))

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self.tokens.append("<str>")
        elif isinstance(node.value, (int, float)):
            self.tokens.append("<num>")
        elif node.value is None:
            self.tokens.append("None")
        elif isinstance(node.value, bool):
            self.tokens.append("<bool>")
        else:
            self.tokens.append("<const>")

    def visit_List(self, node):
        self.tokens.append("[")
        for i, elt in enumerate(node.elts):
            if i > 0:
                self.tokens.append(",")
            self.visit(elt)
        self.tokens.append("]")

    def visit_Tuple(self, node):
        self.tokens.append("(")
        for i, elt in enumerate(node.elts):
            if i > 0:
                self.tokens.append(",")
            self.visit(elt)
        self.tokens.append(")")

    def visit_Dict(self, node):
        self.tokens.append("{")
        for i, (k, v) in enumerate(zip(node.keys, node.values)):
            if i > 0:
                self.tokens.append(",")
            if k is not None:
                self.visit(k)
            self.tokens.append(":")
            self.visit(v)
        self.tokens.append("}")

    def visit_Set(self, node):
        self.tokens.append("{")
        for i, elt in enumerate(node.elts):
            if i > 0:
                self.tokens.append(",")
            self.visit(elt)
        self.tokens.append("}")

    def visit_Subscript(self, node):
        self.visit(node.value)
        self.tokens.append("[")
        self.visit(node.slice)
        self.tokens.append("]")

    def visit_Index(self, node):
        self.visit(node.value)

    def visit_Slice(self, node):
        if node.lower:
            self.visit(node.lower)
        self.tokens.append(":")
        if node.upper:
            self.visit(node.upper)
        if node.step:
            self.tokens.append(":")
            self.visit(node.step)

    def visit_If(self, node):
        self.tokens.append("if")
        self.visit(node.test)
        for child in node.body:
            self.visit(child)
        if node.orelse:
            self.tokens.append("else")
            for child in node.orelse:
                self.visit(child)

    def visit_For(self, node):
        self.tokens.append("for")
        self.visit(node.target)
        self.tokens.append("in")
        self.visit(node.iter)
        for child in node.body:
            self.visit(child)

    def visit_While(self, node):
        self.tokens.append("while")
        self.visit(node.test)
        for child in node.body:
            self.visit(child)

    def visit_Return(self, node):
        self.tokens.append("return")
        if node.value:
            self.visit(node.value)

    def visit_FunctionDef(self, node):
        self.tokens.append("def")
        self.tokens.append("<func>")
        self.tokens.append("(")
        for i, arg in enumerate(node.args.args):
            if i > 0:
                self.tokens.append(",")
            self.tokens.append(self._map_var(arg.arg))
        self.tokens.append(")")
        for child in node.body:
            self.visit(child)

    def visit_AsyncFunctionDef(self, node):
        self.tokens.append("async")
        self.tokens.append("def")
        self.tokens.append("<func>")
        self.tokens.append("(")
        for i, arg in enumerate(node.args.args):
            if i > 0:
                self.tokens.append(",")
            self.tokens.append(self._map_var(arg.arg))
        self.tokens.append(")")
        for child in node.body:
            self.visit(child)

    def visit_ClassDef(self, node):
        self.tokens.append("class")
        self.tokens.append("<class>")
        for child in node.body:
            self.visit(child)

    def visit_Raise(self, node):
        self.tokens.append("raise")
        if node.exc:
            self.visit(node.exc)
        if node.cause:
            self.tokens.append("from")
            self.visit(node.cause)

    def visit_Try(self, node):
        self.tokens.append("try")
        for child in node.body:
            self.visit(child)
        for handler in node.handlers:
            self.tokens.append("except")
            if handler.type:
                self.visit(handler.type)
            for child in handler.body:
                self.visit(child)

    def visit_Import(self, node):
        self.tokens.append("import")
        self.tokens.append("<mod>")

    def visit_ImportFrom(self, node):
        self.tokens.append("from")
        self.tokens.append("<mod>")
        self.tokens.append("import")
        self.tokens.append("<mod>")

    def visit_Pass(self, node):
        pass

    def visit_Break(self, node):
        self.tokens.append("break")

    def visit_Continue(self, node):
        self.tokens.append("continue")

    def visit_Starred(self, node):
        self.tokens.append("*")
        self.visit(node.value)

    def visit_JoinedStr(self, node):
        self.tokens.append("<fstr>")

    def visit_FormattedValue(self, node):
        self.visit(node.value)

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def _op_str(self, op: ast.operator) -> str:
        _MAP = {
            ast.Add: "+",
            ast.Sub: "-",
            ast.Mult: "*",
            ast.Div: "/",
            ast.FloorDiv: "//",
            ast.Mod: "%",
            ast.Pow: "**",
            ast.LShift: "<<",
            ast.RShift: ">>",
            ast.BitOr: "|",
            ast.BitXor: "^",
            ast.BitAnd: "&",
            ast.MatMult: "@",
        }
        return _MAP.get(type(op), type(op).__name__)

    def _cmpop_str(self, op: ast.cmpop) -> str:
        _MAP = {
            ast.Eq: "==",
            ast.NotEq: "!=",
            ast.Lt: "<",
            ast.LtE: "<=",
            ast.Gt: ">",
            ast.GtE: ">=",
            ast.Is: "is",
            ast.IsNot: "is not",
            ast.In: "in",
            ast.NotIn: "not in",
        }
        return _MAP.get(type(op), type(op).__name__)

    def _boolop_str(self, op: ast.boolop) -> str:
        if isinstance(op, ast.And):
            return "and"
        return "or"


class _NodeSequenceExtractor(ast.NodeVisitor):
    def __init__(self):
        self.sequence: List[str] = []

    def generic_visit(self, node):
        self.sequence.append(type(node).__name__)
        for child in ast.iter_child_nodes(node):
            self.visit(child)


class _OperatorExtractor(ast.NodeVisitor):
    def __init__(self):
        self.operators: List[str] = []

    def visit_BinOp(self, node):
        self.operators.append(type(node.op).__name__)
        self.visit(node.left)
        self.visit(node.right)

    def visit_UnaryOp(self, node):
        self.operators.append(type(node.op).__name__)
        self.visit(node.operand)

    def visit_AugAssign(self, node):
        self.operators.append(type(node.op).__name__)
        self.visit(node.target)
        self.visit(node.value)

    def visit_Compare(self, node):
        for op in node.ops:
            self.operators.append(type(op).__name__)
        self.visit(node.left)
        for comp in node.comparators:
            self.visit(comp)

    def visit_BoolOp(self, node):
        self.operators.append(type(node.op).__name__)
        for value in node.values:
            self.visit(value)

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            self.visit(child)


class _SharingNormalizer(ast.NodeTransformer):
    def __init__(self):
        self._var_map: Dict[str, str] = {}
        self._var_counter: int = 0
        self._mod_counter: int = 0

    def _map_var(self, name: str) -> str:
        if name in self._var_map:
            return self._var_map[name]
        mapped = f"_v{self._var_counter}"
        self._var_counter += 1
        self._var_map[name] = mapped
        return mapped

    def visit_Name(self, node):
        node.id = self._map_var(node.id)
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            node.value = "<str>"
        elif isinstance(node.value, (int, float)):
            node.value = 0
        return node

    def visit_Import(self, node):
        mod_name = f"_mod{self._mod_counter}"
        self._mod_counter += 1
        return ast.Import(names=[ast.alias(name=mod_name)])

    def visit_ImportFrom(self, node):
        mod_name = f"_mod{self._mod_counter}"
        self._mod_counter += 1
        return ast.ImportFrom(module=mod_name, names=[ast.alias(name="_imp")], level=0)
