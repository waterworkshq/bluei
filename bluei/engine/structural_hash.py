"""Structural code fingerprinting for duplicate/similar pattern detection.

Normalizes AST and text to structure-only tokens, then hashes for exact
matching or computes similarity via Levenshtein distance. Used by the
pattern replay system.
"""
import ast
import hashlib
import re
from typing import Dict, List, Optional, Tuple

FUZZY_THRESHOLDS: Dict[str, float] = {
    "bug": 0.85,
    "lint": 0.70,
    "type-safety": 0.80,
    "test-coverage": 0.60,
    "test-gap": 0.60,
    "refactor": 0.75,
    "perf-smell": 0.80,
    "security": 0.85,
    "style": 0.65,
    "docs-gap": 0.65,
    "docs-drift": 0.65,
    "docs-mismatch": 0.65,
    "todo/debt": 0.70,
}

DEFAULT_FUZZY_THRESHOLD = 0.75


def compute_structural_hash(code: str, language: str) -> str:
    if language in ("python",):
        return _python_structural_hash(code)
    return _text_structural_hash(code)


def _python_structural_hash(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return _text_structural_hash(code)
    normalizer = _PythonStructuralNormalizer()
    normalizer.visit(tree)
    return hashlib.sha256(" ".join(normalizer.tokens).encode("utf-8")).hexdigest()[:16]


def _text_structural_hash(code: str) -> str:
    normalized = _text_normalize(code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _text_normalize(code: str) -> str:
    text = code
    text = re.sub(r'#.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'//.*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    text = re.sub(r'"(?:[^"\\]|\\.)*"', '"<str>"', text)
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "'<str>'", text)
    text = re.sub(r'\b\d+\.?\d*\b', '<num>', text)
    text = re.sub(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', '<id>', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


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
            ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
            ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
            ast.LShift: "<<", ast.RShift: ">>", ast.BitOr: "|",
            ast.BitXor: "^", ast.BitAnd: "&", ast.MatMult: "@",
        }
        return _MAP.get(type(op), type(op).__name__)

    def _cmpop_str(self, op: ast.cmpop) -> str:
        _MAP = {
            ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
            ast.Gt: ">", ast.GtE: ">=", ast.Is: "is", ast.IsNot: "is not",
            ast.In: "in", ast.NotIn: "not in",
        }
        return _MAP.get(type(op), type(op).__name__)

    def _boolop_str(self, op: ast.boolop) -> str:
        if isinstance(op, ast.And):
            return "and"
        return "or"


def extract_node_sequence(code: str, language: str) -> List[str]:
    if language in ("python",):
        return _python_node_sequence(code)
    return _text_node_sequence(code)


def _python_node_sequence(code: str) -> List[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return _text_node_sequence(code)
    visitor = _NodeSequenceExtractor()
    visitor.visit(tree)
    return visitor.sequence


class _NodeSequenceExtractor(ast.NodeVisitor):
    def __init__(self):
        self.sequence: List[str] = []

    def generic_visit(self, node):
        self.sequence.append(type(node).__name__)
        for child in ast.iter_child_nodes(node):
            self.visit(child)


def _text_node_sequence(code: str) -> List[str]:
    tokens = []
    for m in re.finditer(r'[a-zA-Z_]\w*|[+\-*/%=<>!&|^~]+|[{}\[\](),.;:]', code):
        tok = m.group()
        if re.match(r'^[a-zA-Z_]', tok):
            tokens.append("ID")
        else:
            tokens.append(tok)
    return tokens


def extract_operator_sequence(code: str, language: str) -> List[str]:
    if language in ("python",):
        return _python_operator_sequence(code)
    return _text_operator_sequence(code)


def _python_operator_sequence(code: str) -> List[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return _text_operator_sequence(code)
    extractor = _OperatorExtractor()
    extractor.visit(tree)
    return extractor.operators


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


def _text_operator_sequence(code: str) -> List[str]:
    ops = []
    for m in re.finditer(r'[+\-*/%=<>!&|^~]+', code):
        ops.append(m.group())
    return ops


def sequence_similarity(seq_a: List[str], seq_b: List[str]) -> float:
    if not seq_a and not seq_b:
        return 1.0
    if not seq_a or not seq_b:
        return 0.0
    la, lb = len(seq_a), len(seq_b)
    if abs(la - lb) > max(la, lb):
        return 0.0
    dist = _levenshtein_distance(seq_a, seq_b)
    max_len = max(la, lb)
    if max_len == 0:
        return 1.0
    return 1.0 - (dist / max_len)


def _levenshtein_distance(a: List[str], b: List[str]) -> int:
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)
    for i in range(1, la + 1):
        curr[0] = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + cost,
            )
        prev, curr = curr, prev
    return prev[lb]


def fuzzy_structural_match(
    finding_snippet: str,
    pattern_before: str,
    language: str,
) -> float:
    h1 = compute_structural_hash(finding_snippet, language)
    h2 = compute_structural_hash(pattern_before, language)
    if h1 == h2:
        return 1.0
    nodes_a = extract_node_sequence(finding_snippet, language)
    nodes_b = extract_node_sequence(pattern_before, language)
    node_sim = sequence_similarity(nodes_a, nodes_b)
    ops_a = extract_operator_sequence(finding_snippet, language)
    ops_b = extract_operator_sequence(pattern_before, language)
    if ops_a or ops_b:
        op_sim = sequence_similarity(ops_a, ops_b)
        return 0.4 * node_sim + 0.6 * op_sim
    return node_sim


def get_fuzzy_threshold(rule: str, catalog: Optional[List[Dict]] = None) -> float:
    if catalog:
        for entry in catalog:
            if entry.get("rule") == rule:
                cat = entry.get("category", "")
                return FUZZY_THRESHOLDS.get(cat, DEFAULT_FUZZY_THRESHOLD)
    return DEFAULT_FUZZY_THRESHOLD


def normalize_for_sharing(code: str, language: str) -> str:
    if language in ("python",):
        return _python_normalize_for_sharing(code)
    return _text_normalize_for_sharing(code)


def _python_normalize_for_sharing(code: str) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return _text_normalize_for_sharing(code)
    normalizer = _SharingNormalizer()
    normalizer.visit(tree)
    return ast.unparse(tree)


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


def _text_normalize_for_sharing(code: str) -> str:
    return _text_normalize(code)
