"""Tree-sitter matcher for TS/Go/Rust with ts_*/go_*/rust_* constraint keys."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ASTMatch, ASTPattern
from .ts_parser import TSNode, TreeSitterAdapter



class TSPatternMatcher:
    def __init__(self, patterns: Optional[List[ASTPattern]] = None):
        self.patterns: List[ASTPattern] = patterns or []

    def find_matches(
        self, source: str, file_path: str, language: str = "typescript"
    ) -> List[ASTMatch]:
        adapter = TreeSitterAdapter(language)
        root = adapter.parse(source)
        if root is None:
            return []

        matches: List[ASTMatch] = []
        applicable = [p for p in self.patterns if p.language == language]

        for pattern in applicable:
            found_nodes = root.find_all(pattern.node_type)
            for ts_node in found_nodes:
                if self._matches(ts_node, pattern, file_path):
                    matches.append(
                        ASTMatch(
                            pattern=pattern,
                            node=ts_node,
                            line=ts_node.start_row + 1,
                            col=ts_node.start_col,
                            source_text=ts_node.text,
                            context={"file_path": file_path},
                        )
                    )

        return matches

    def _matches(self, node: TSNode, pattern: ASTPattern, file_path: str) -> bool:
        constraints = pattern.constraints or {}
        for key, value in constraints.items():
            if not self._check_constraint(node, key, value, file_path):
                return False

        negations = pattern.negation_constraints or {}
        for key, value in negations.items():
            if self._check_constraint(node, key, value, file_path):
                return False

        return True

    def _check_constraint(self, node: TSNode, key: str, value: Any, file_path: str) -> bool:
        if key == "ts_node_child_type":
            for child in node.children:
                if child.type == value:
                    return True
            return False

        if key == "ts_node_child_text":
            for child in node.children:
                if child.text.strip() == value:
                    return True
            return False

        if key == "ts_call_name":
            func_node = node.child_by_field_name("function")
            if func_node is None:
                return False
            return func_node.text == value

        if key == "ts_right_type":
            for child in node.children:
                if child.type == "type_identifier" and child.text == value:
                    return True
                if child.type == "predefined_type" and child.text == value:
                    return True
            return False

        if key == "ts_missing_return_type":
            return_node = node.child_by_field_name("return_type")
            result_node = node.child_by_field_name("result")
            return return_node is None and result_node is None

        if key == "ts_is_exported":
            parent = node.parent
            if parent and parent.type == "export_statement":
                return True
            return False

        if key == "ts_comment_pattern":
            return bool(re.search(value, node.text))

        if key == "ts_call_returns_promise":
            text = node.text
            return "Promise" in text or ".then(" in text or ".catch(" in text

        if key == "ts_has_catch":
            parent = node.parent
            while parent:
                if parent.type == "try_statement":
                    return True
                parent = parent.parent
            text = node.text
            if ".catch(" in text or ".catch (" in text:
                return True
            return False

        if key == "ts_is_awaited":
            parent = node.parent
            while parent:
                if parent.type == "await_expression":
                    return True
                parent = parent.parent
            return False

        if key == "in_test_file":
            fp = Path(file_path)
            name = fp.name.lower()
            return (
                "test" in name
                or "spec" in name
                or "__test__" in str(fp)
                or "_test" in name
            )

        if key == "go_rhs_is_call":
            for child in node.children:
                if child.type == "call_expression":
                    return True
                if child.type == "expression_list":
                    for subchild in child.children:
                        if subchild.type == "call_expression":
                            return True
                right = child.child_by_field_name("right")
                if right and right.type == "call_expression":
                    return True
            return False

        if key == "go_lhs_has_error_var":
            for child in node.children:
                if child.type in ("expression_list", "identifier"):
                    if "err" in child.text.lower():
                        return True
                left = child.child_by_field_name("left")
                if left and "err" in left.text.lower():
                    return True
            if len(node.children) >= 1:
                first = node.children[0]
                if first.type == "expression_list":
                    for sub in first.children:
                        if "err" in sub.text.lower():
                            return True
            return False

        if key == "go_has_defer_recover":
            parent = node.parent
            while parent:
                for child in parent.children:
                    if child.type == "defer_statement":
                        if "recover" in child.text:
                            return True
                parent = parent.parent
            return False

        if key == "go_interface_empty":
            body_children = [c for c in node.children if c.type not in ("{", "}", "interface")]
            return len(body_children) == 0

        if key == "rust_method_name":
            func_node = node.child_by_field_name("function")
            if func_node is None:
                return False
            if func_node.type == "field_identifier":
                return func_node.text == value
            return func_node.text.endswith("." + value)

        if key == "rust_arg_empty_or_vague":
            args_node = node.child_by_field_name("arguments")
            if args_node is None:
                return False
            arg_text = args_node.text.strip().strip("()")
            if not arg_text or arg_text == '""' or arg_text == '"error"':
                return True
            return False

        return False
