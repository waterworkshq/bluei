"""Uniform tree-sitter adapter wrapping TS/Go/Rust parsers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)



class TSNode:
    __slots__ = ("_node", "type", "text", "start_row", "start_col", "end_row", "end_col", "children", "parent")

    def __init__(self, node, source_bytes: bytes = b"", parent: Optional["TSNode"] = None):
        self._node = node
        self.type: str = node.type
        self.text: str = node.text.decode("utf-8") if hasattr(node.text, "decode") else str(node.text)
        self.start_row: int = node.start_point[0]
        self.start_col: int = node.start_point[1]
        self.end_row: int = node.end_point[0]
        self.end_col: int = node.end_point[1]
        self.parent: Optional["TSNode"] = parent
        self.children: List["TSNode"] = [TSNode(c, parent=self) for c in node.children]

    def child_by_field_name(self, name: str) -> Optional["TSNode"]:
        child = self._node.child_by_field_name(name)
        if child is None:
            return None
        return TSNode(child, parent=self)

    def children_by_type(self, type_name: str) -> List["TSNode"]:
        return [TSNode(c, parent=self) for c in self._node.children if c.type == type_name]

    def find_all(self, type_name: str) -> List["TSNode"]:
        results: List["TSNode"] = []
        self._find_all_recursive(self._node, type_name, results, self)
        return results

    def _find_all_recursive(self, node, type_name: str, results: list, parent: "TSNode"):
        if node.type == type_name:
            results.append(TSNode(node, parent=parent))
        for child in node.children:
            wrapper = TSNode(child, parent=parent)
            self._find_all_recursive(child, type_name, results, wrapper)

    def __repr__(self):
        return f"TSNode({self.type}, line={self.start_row + 1})"


class TreeSitterAdapter:
    _parsers: Dict[str, Any] = {}

    def __init__(self, language: str):
        self.language = language
        self._parser = self._get_parser(language)

    @staticmethod
    def is_available() -> bool:
        try:
            import tree_sitter  # noqa: F401
            return True
        except ImportError:
            return False

    def parse(self, source: str) -> Optional[TSNode]:
        if self._parser is None:
            return None
        try:
            tree = self._parser.parse(source.encode("utf-8"))
            return TSNode(tree.root_node)
        except Exception:
            return None

    def _get_parser(self, language: str):
        if language in self._parsers:
            return self._parsers[language]

        try:
            from tree_sitter import Language, Parser

            lang_map = {}
            try:
                import tree_sitter_typescript as tstypescript
                if language in ("typescript", "tsx"):
                    ts_lang = Language(tstypescript.language_typescript())
                    lang_map["typescript"] = ts_lang
                    lang_map["tsx"] = ts_lang
                elif language in ("javascript", "jsx"):
                    js_lang = Language(tstypescript.language_typescript())
                    lang_map["javascript"] = js_lang
                    lang_map["jsx"] = js_lang
            except ImportError:
                _logger.debug("tree-sitter-typescript not available")

            try:
                import tree_sitter_javascript as tsjs
                js_lang = Language(tsjs.language())
                if "javascript" not in lang_map:
                    lang_map["javascript"] = js_lang
                    lang_map["jsx"] = js_lang
            except ImportError:
                _logger.debug("tree-sitter-javascript not available")

            try:
                import tree_sitter_go as tsgo
                lang_map["go"] = Language(tsgo.language())
            except ImportError:
                _logger.debug("tree-sitter-go not available")

            try:
                import tree_sitter_rust as tsrust
                lang_map["rust"] = Language(tsrust.language())
            except ImportError:
                _logger.debug("tree-sitter-rust not available")

            if language in lang_map:
                parser = Parser(lang_map[language])
                self._parsers[language] = parser
                return parser
        except ImportError:
            _logger.debug("tree-sitter not available")

        self._parsers[language] = None
        return None
