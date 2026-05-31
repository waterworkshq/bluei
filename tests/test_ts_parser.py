"""Tests for bluei.engine.ast_engine.ts_parser — TreeSitterAdapter and TSNode."""

import pytest

from bluei.engine.ast_engine.ts_parser import TSNode, TreeSitterAdapter


# ---------------------------------------------------------------------------
# TreeSitterAdapter — availability
# ---------------------------------------------------------------------------


class TestTreeSitterAvailability:
    def test_is_available_returns_true(self):
        """Tree-sitter is installed on this system."""
        assert TreeSitterAdapter.is_available() is True


# ---------------------------------------------------------------------------
# TreeSitterAdapter — unsupported languages
# ---------------------------------------------------------------------------


class TestTreeSitterUnsupportedLanguage:
    def test_unsupported_language_parser_is_none(self):
        """Languages without a tree-sitter grammar return None parser."""
        adapter = TreeSitterAdapter("python")
        assert adapter._parser is None

    def test_parse_unsupported_language_returns_none(self):
        """Parsing with an unsupported language yields None."""
        adapter = TreeSitterAdapter("python")
        assert adapter.parse("x = 1") is None

    def test_parse_empty_string_unsupported(self):
        adapter = TreeSitterAdapter("cobol")
        assert adapter.parse("") is None


# ---------------------------------------------------------------------------
# TreeSitterAdapter — TypeScript parsing
# ---------------------------------------------------------------------------


class TestTreeSitterTypeScript:
    def test_parse_typescript_root_node(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1 + 2;")
        assert node is not None
        assert node.type == "program"

    def test_parse_typescript_children(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        assert len(node.children) > 0
        assert node.children[0].type == "lexical_declaration"

    def test_parse_typescript_empty_source(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("")
        # tree-sitter returns a root node even for empty input
        assert node is not None
        assert node.type == "program"

    def test_parse_typescript_invalid_syntax(self):
        """Partial/invalid source still produces a tree with ERROR nodes."""
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("function ( { }}}")
        assert node is not None
        assert node.type == "program"
        # Tree-sitter produces error nodes instead of returning None
        error_nodes = node.find_all("ERROR")
        assert len(error_nodes) > 0


# ---------------------------------------------------------------------------
# TreeSitterAdapter — Go parsing
# ---------------------------------------------------------------------------


class TestTreeSitterGo:
    def test_parse_go_root_node(self):
        adapter = TreeSitterAdapter("go")
        node = adapter.parse('package main\nfunc main() { fmt.Println("hi") }')
        assert node is not None
        assert node.type == "source_file"

    def test_parse_go_function_declaration(self):
        adapter = TreeSitterAdapter("go")
        node = adapter.parse(
            "package main\nfunc add(a int, b int) int { return a + b }"
        )
        funcs = node.find_all("function_declaration")
        assert len(funcs) >= 1

    def test_parse_go_empty_source(self):
        adapter = TreeSitterAdapter("go")
        node = adapter.parse("")
        assert node is not None
        assert node.type == "source_file"


# ---------------------------------------------------------------------------
# TreeSitterAdapter — Rust parsing
# ---------------------------------------------------------------------------


class TestTreeSitterRust:
    def test_parse_rust_root_node(self):
        adapter = TreeSitterAdapter("rust")
        node = adapter.parse("fn main() { let x = 1; }")
        assert node is not None
        assert node.type == "source_file"

    def test_parse_rust_let_binding(self):
        adapter = TreeSitterAdapter("rust")
        node = adapter.parse("fn main() { let x: i32 = 42; }")
        let_nodes = node.find_all("let_declaration")
        assert len(let_nodes) >= 1


# ---------------------------------------------------------------------------
# TSNode — attributes
# ---------------------------------------------------------------------------


class TestTSNodeAttributes:
    def test_node_type_and_text(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        assert node.type == "program"
        assert "let x = 1;" in node.text

    def test_node_start_row_and_col(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        # Root node starts at line 0, column 0 (0-indexed in tree-sitter)
        assert node.start_row == 0
        assert node.start_col == 0

    def test_node_end_row_and_col(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        assert node.end_row >= 0
        assert node.end_col >= 0

    def test_node_children_are_tsnode_instances(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        for child in node.children:
            assert isinstance(child, TSNode)


# ---------------------------------------------------------------------------
# TSNode — child_by_field_name
# ---------------------------------------------------------------------------


class TestTSNodeChildByFieldName:
    def test_returns_correct_child(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1 + 2;")
        var_decls = node.find_all("variable_declarator")
        assert len(var_decls) >= 1
        name_node = var_decls[0].child_by_field_name("name")
        assert name_node is not None
        assert name_node.text.strip() == "x"

    def test_returns_none_for_missing_field(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        var_decl = node.find_all("variable_declarator")[0]
        assert var_decl.child_by_field_name("nonexistent_field") is None

    def test_returns_none_on_leaf_node(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        identifiers = node.find_all("identifier")
        assert len(identifiers) >= 1
        # Identifier is a leaf — no named children by field
        assert identifiers[0].child_by_field_name("name") is None


# ---------------------------------------------------------------------------
# TSNode — children_by_type
# ---------------------------------------------------------------------------


class TestTSNodeChildrenByType:
    def test_filters_correctly(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1; let y = 2;")
        root = node
        decls = root.children_by_type("lexical_declaration")
        assert len(decls) >= 1
        for d in decls:
            assert d.type == "lexical_declaration"

    def test_returns_empty_list_for_absent_type(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        classes = node.children_by_type("class_declaration")
        assert classes == []

    def test_does_not_recurse_only_direct_children(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1 + 2;")
        # "identifier" exists deep in the tree but not as direct child of program
        ids = node.children_by_type("identifier")
        assert ids == []


# ---------------------------------------------------------------------------
# TSNode — find_all
# ---------------------------------------------------------------------------


class TestTSNodeFindAll:
    def test_finds_nodes_recursively(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = a + b;")
        identifiers = node.find_all("identifier")
        id_texts = [i.text.strip() for i in identifiers]
        assert "x" in id_texts
        assert "a" in id_texts
        assert "b" in id_texts

    def test_returns_empty_for_absent_type(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        assert node.find_all("class_declaration") == []

    def test_finds_deeply_nested_nodes(self):
        adapter = TreeSitterAdapter("typescript")
        source = "function foo() { if (true) { let y = bar(); } }"
        node = adapter.parse(source)
        calls = node.find_all("call_expression")
        assert len(calls) >= 1


# ---------------------------------------------------------------------------
# TSNode — __repr__
# ---------------------------------------------------------------------------


class TestTSNodeRepr:
    def test_repr_format(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        r = repr(node)
        assert "TSNode" in r
        assert "program" in r
        assert "line=" in r

    def test_repr_line_is_one_indexed(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        # start_row is 0, repr should show line=1
        assert repr(node) == "TSNode(program, line=1)"

    def test_repr_on_child_node(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        child = node.children[0]
        r = repr(child)
        assert "TSNode" in r
        assert child.type in r


# ---------------------------------------------------------------------------
# TSNode — parent tracking
# ---------------------------------------------------------------------------


class TestTSNodeParent:
    def test_root_parent_is_none(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        assert node.parent is None

    def test_child_parent_links_to_wrapper(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        assert len(node.children) > 0
        child = node.children[0]
        assert child.parent is node

    def test_grandchild_parent_chain(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        identifiers = node.find_all("identifier")
        assert len(identifiers) >= 1
        ident = identifiers[0]
        assert ident.parent is not None
        assert ident.parent.parent is not None
        # Walk up to root
        root = ident.parent.parent
        while root.parent is not None:
            root = root.parent
        assert root.type == "program"

    def test_child_by_field_name_parent(self):
        adapter = TreeSitterAdapter("typescript")
        node = adapter.parse("let x = 1;")
        var_decl = node.find_all("variable_declarator")[0]
        name = var_decl.child_by_field_name("name")
        assert name is not None
        assert name.parent is not None


# ---------------------------------------------------------------------------
# TreeSitterAdapter — error handling in parse
# ---------------------------------------------------------------------------


class TestTreeSitterParseErrors:
    def test_parse_exception_returns_none(self):
        """Line 70-71: parse() catches exception from parser.parse()."""
        adapter = TreeSitterAdapter("typescript")

        # tree_sitter.Parser.parse is a read-only C attribute, so replace
        # the entire parser with a fake that raises.
        class _FakeParser:
            def parse(self, source):
                raise RuntimeError("tree-sitter died")

        adapter._parser = _FakeParser()
        assert adapter.parse("let x = 1;") is None


# ---------------------------------------------------------------------------
# TreeSitterAdapter — parser caching
# ---------------------------------------------------------------------------


class TestTreeSitterParserCaching:
    def test_same_language_reuses_cached_parser(self):
        """Parser instances are cached by language name."""
        a1 = TreeSitterAdapter("typescript")
        a2 = TreeSitterAdapter("typescript")
        assert a1._parser is a2._parser

    def test_cached_unsupported_language_stays_none(self):
        """Once an unsupported language is cached as None, it stays None."""
        a1 = TreeSitterAdapter("brainfuck")
        assert a1._parser is None
        a2 = TreeSitterAdapter("brainfuck")
        assert a2._parser is None


# ---------------------------------------------------------------------------
# TreeSitterAdapter — TSX
# ---------------------------------------------------------------------------


class TestTreeSitterTSX:
    def test_parse_tsx(self):
        adapter = TreeSitterAdapter("tsx")
        node = adapter.parse("const x = <div>hello</div>;")
        assert node is not None
        assert node.type == "program"


# ---------------------------------------------------------------------------
# TreeSitterAdapter — JavaScript branch (lines 88-90)
# ---------------------------------------------------------------------------


class TestTreeSitterJavaScript:
    def test_parse_javascript_root_node(self):
        """Lines 88-90: JavaScript branch of typescript import."""
        adapter = TreeSitterAdapter("javascript")
        node = adapter.parse("var x = 1 + 2;")
        assert node is not None
        assert node.type == "program"


# ---------------------------------------------------------------------------
# TreeSitterAdapter — import fallback paths
# ---------------------------------------------------------------------------


class TestTreeSitterImportFallbacks:
    def test_javascript_package_import_error(self, monkeypatch):
        """Lines 100-101: tree_sitter_javascript ImportError fallback."""
        import sys

        TreeSitterAdapter._parsers.pop("javascript", None)
        # Set to None (not pop) so reimport also fails
        monkeypatch.setitem(sys.modules, "tree_sitter_javascript", None)
        adapter = TreeSitterAdapter("javascript")
        # TypeScript package already provides JS support
        assert adapter._parser is not None

    def test_typescript_import_error_returns_none(self, monkeypatch):
        """Lines 91-92: tree_sitter_typescript ImportError falls back."""
        import sys

        lang_key = "_test_no_ts_typescript"
        TreeSitterAdapter._parsers.pop(lang_key, None)
        monkeypatch.setitem(sys.modules, "tree_sitter_typescript", None)
        adapter = TreeSitterAdapter(lang_key)
        assert adapter._parser is None

    def test_go_import_error_returns_none(self, monkeypatch):
        """Lines 106-107: tree_sitter_go ImportError falls back to None."""
        import sys

        TreeSitterAdapter._parsers.pop("go", None)
        monkeypatch.setitem(sys.modules, "tree_sitter_go", None)
        adapter = TreeSitterAdapter("go")
        assert adapter._parser is None

    def test_rust_import_error_returns_none(self, monkeypatch):
        """Lines 112-113: tree_sitter_rust ImportError falls back to None."""
        import sys

        TreeSitterAdapter._parsers.pop("rust", None)
        monkeypatch.setitem(sys.modules, "tree_sitter_rust", None)
        adapter = TreeSitterAdapter("rust")
        assert adapter._parser is None

    def test_outer_import_error_returns_none(self, monkeypatch):
        """Lines 119-120: outer ImportError when tree_sitter itself is gone."""
        import sys

        lang_key = "_test_no_ts"
        TreeSitterAdapter._parsers.pop(lang_key, None)
        monkeypatch.setitem(sys.modules, "tree_sitter", None)
        adapter = TreeSitterAdapter(lang_key)
        assert adapter._parser is None


# ---------------------------------------------------------------------------
# TreeSitterAdapter — is_available fallback
# ---------------------------------------------------------------------------


class TestTreeSitterIsAvailableFallback:
    def test_is_available_false_when_missing(self, monkeypatch):
        """Lines 61-62: is_available returns False when tree_sitter missing."""
        import sys

        monkeypatch.setitem(sys.modules, "tree_sitter", None)
        assert TreeSitterAdapter.is_available() is False

    def test_rust_import_error_returns_none(self, monkeypatch):
        """Lines 112-113: tree_sitter_rust ImportError falls back to None."""
        import sys

        TreeSitterAdapter._parsers.pop("rust", None)
        monkeypatch.setitem(sys.modules, "tree_sitter_rust", None)
        adapter = TreeSitterAdapter("rust")
        assert adapter._parser is None

    def test_outer_import_error_returns_none(self, monkeypatch):
        """Lines 119-120: outer ImportError when tree_sitter itself is gone."""
        import sys

        lang_key = "_test_no_ts"
        TreeSitterAdapter._parsers.pop(lang_key, None)
        monkeypatch.setitem(sys.modules, "tree_sitter", None)
        adapter = TreeSitterAdapter(lang_key)
        assert adapter._parser is None
