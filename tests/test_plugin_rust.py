"""Tests for plugins.rust.plugin — cargo clippy wrapper and TODO scanning."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def plugin():
    from plugins.rust.plugin import Plugin

    return Plugin()


class TestRustPluginProperties:
    """Basic plugin metadata."""

    def test_plugin_properties(self, plugin) -> None:
        assert plugin.id == "plugin-rust"
        assert plugin.name == "Rust Plugin"
        assert "rust" in plugin.languages
        assert "clippy-unwrap-used" in plugin.rules


class TestRustPluginParseOutput:
    """Tests for Plugin.parse_output."""

    def test_parse_output_empty(self, plugin, tmp_path: Path) -> None:
        result = plugin.parse_output("", tmp_path)
        assert result == []

    def test_parse_output_compiler_message(self, plugin, tmp_path: Path) -> None:
        clippy_json = json.dumps(
            {
                "reason": "compiler-message",
                "message": {
                    "code": {"code": "clippy::unwrap-used"},
                    "spans": [{"file_name": "src/main.rs", "line_start": 42}],
                    "rendered": "called unwrap on an Option",
                },
            }
        )
        result = plugin.parse_output(clippy_json, tmp_path)
        assert len(result) == 1
        assert result[0].rule == "clippy-unwrap-used"
        assert result[0].line == 42

    def test_parse_output_skips_non_compiler_message(
        self, plugin, tmp_path: Path
    ) -> None:
        line = json.dumps({"reason": "compiler-artifact", "target": {}})
        result = plugin.parse_output(line, tmp_path)
        assert result == []

    def test_parse_output_skips_no_code(self, plugin, tmp_path: Path) -> None:
        line = json.dumps(
            {
                "reason": "compiler-message",
                "message": {
                    "code": {},
                    "spans": [{"file_name": "x.rs", "line_start": 1}],
                },
            }
        )
        result = plugin.parse_output(line, tmp_path)
        assert result == []

    def test_parse_output_skips_no_spans(self, plugin, tmp_path: Path) -> None:
        line = json.dumps(
            {
                "reason": "compiler-message",
                "message": {"code": {"code": "clippy::foo"}, "spans": []},
            }
        )
        result = plugin.parse_output(line, tmp_path)
        assert result == []

    def test_parse_output_skips_invalid_json(self, plugin, tmp_path: Path) -> None:
        result = plugin.parse_output("not json at all", tmp_path)
        assert result == []

    def test_parse_output_multiple_messages(self, plugin, tmp_path: Path) -> None:
        lines = [
            json.dumps(
                {
                    "reason": "compiler-message",
                    "message": {
                        "code": {"code": "clippy::unwrap-used"},
                        "spans": [{"file_name": "a.rs", "line_start": 1}],
                        "rendered": "msg1",
                    },
                }
            ),
            json.dumps(
                {
                    "reason": "compiler-message",
                    "message": {
                        "code": {"code": "clippy::clone-on-copy"},
                        "spans": [{"file_name": "b.rs", "line_start": 2}],
                        "rendered": "msg2",
                    },
                }
            ),
        ]
        result = plugin.parse_output("\n".join(lines), tmp_path)
        assert len(result) == 2


class TestRustPluginScanTodos:
    """Tests for Plugin._scan_todos."""

    def test_scan_todos(self, plugin, tmp_path: Path) -> None:
        rs_file = tmp_path / "src" / "main.rs"
        rs_file.parent.mkdir(parents=True)
        rs_file.write_text(
            "fn main() {\n    // TODO: fix this\n    // FIXME: also this\n}\n"
        )
        result = plugin._scan_todos(tmp_path)
        assert len(result) == 2
        assert result[0].rule == "debt-todo-marker"
        assert result[1].rule == "debt-todo-marker"

    def test_scan_todos_empty_dir(self, plugin, tmp_path: Path) -> None:
        result = plugin._scan_todos(tmp_path)
        assert result == []

    def test_scan_todos_unreadable_file(self, plugin, tmp_path: Path) -> None:
        rs_file = tmp_path / "bad.rs"
        rs_file.write_text("fn main() {}")
        rs_file.chmod(0o000)
        try:
            result = plugin._scan_todos(tmp_path)
            assert result == []
        finally:
            rs_file.chmod(0o644)


class TestRustPluginDiscover:
    """Tests for Plugin.discover."""

    def test_discover_uses_tool_output(self, plugin, tmp_path: Path) -> None:
        clippy_output = json.dumps(
            {
                "reason": "compiler-message",
                "message": {
                    "code": {"code": "clippy::clone-on-copy"},
                    "spans": [{"file_name": "src/lib.rs", "line_start": 10}],
                    "rendered": "using clone on copy type",
                },
            }
        )
        with patch.object(plugin, "_run_tool", return_value=clippy_output):
            result = plugin.discover(tmp_path, {})
        assert len(result) == 1
        assert result[0].rule == "clippy-clone-on-copy"

    def test_discover_falls_back_to_todos(self, plugin, tmp_path: Path) -> None:
        rs_file = tmp_path / "main.rs"
        rs_file.write_text("// TODO: implement\n")
        with patch.object(plugin, "_run_tool", return_value=None):
            result = plugin.discover(tmp_path, {})
        assert len(result) == 1
        assert result[0].rule == "debt-todo-marker"


class TestStableFindingId:
    """Tests for stable_finding_id."""

    def test_deterministic(self) -> None:
        from plugins.rust.plugin import stable_finding_id

        id1 = stable_finding_id("repo", "path", 1, "rule", "snippet")
        id2 = stable_finding_id("repo", "path", 1, "rule", "snippet")
        assert id1 == id2
        assert len(id1) == 64

    def test_different_for_different_input(self) -> None:
        from plugins.rust.plugin import stable_finding_id

        id1 = stable_finding_id("repo", "path", 1, "rule", "snippet")
        id2 = stable_finding_id("repo", "path", 2, "rule", "snippet")
        assert id1 != id2


# ---------------------------------------------------------------------------
# Behavior-focused additions: realistic clippy output, finding fields, edge cases
# ---------------------------------------------------------------------------


def _clippy_message(code: str, file_name: str, line_start: int, rendered: str) -> str:
    """Helper: build a single clippy compiler-message JSON line."""
    return json.dumps(
        {
            "reason": "compiler-message",
            "message": {
                "code": {"code": f"clippy::{code}"},
                "spans": [{"file_name": file_name, "line_start": line_start}],
                "rendered": rendered,
            },
        }
    )


def _artifact_line() -> str:
    """Helper: a compiler-artifact line that must be skipped."""
    return json.dumps(
        {"reason": "compiler-artifact", "target": {"name": "mycrate", "kind": ["lib"]}}
    )


class TestClippyRealisticOutput:
    """Parse realistic cargo clippy JSON with mixed artifact and diagnostic lines."""

    @pytest.mark.parametrize(
        "code,expected_rule",
        [
            ("unwrap-used", "clippy-unwrap-used"),
            ("clone-on-copy", "clippy-clone-on-copy"),
            ("needless-range-loop", "clippy-needless-range-loop"),
            ("redundant-clone", "clippy-redundant-clone"),
            ("dead-code", "clippy-dead-code"),
            ("unused-imports", "clippy-unused-imports"),
            ("doc-markdown", "clippy-doc-markdown"),
            ("panic", "clippy-panic"),
            ("expect-used", "clippy-expect-used"),
        ],
    )
    def test_various_clippy_codes_map_to_rules(
        self, plugin, tmp_path, code, expected_rule
    ) -> None:
        """Each clippy::code should produce a finding with rule clippy-{code}."""
        output = _clippy_message(code, "src/lib.rs", 7, f"warning: {code}")
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == expected_rule

    def test_mixed_artifact_and_diagnostic_lines(self, plugin, tmp_path) -> None:
        """Artifacts should be skipped; only diagnostics produce findings."""
        output = "\n".join(
            [
                _artifact_line(),
                _clippy_message("unwrap-used", "src/main.rs", 10, "called `.unwrap()`"),
                _artifact_line(),
                _clippy_message("clone-on-copy", "src/lib.rs", 22, "cloning copy type"),
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 2
        rules = {f.rule for f in findings}
        assert "clippy-unwrap-used" in rules
        assert "clippy-clone-on-copy" in rules

    def test_finding_field_values_from_clippy(self, plugin, tmp_path) -> None:
        """Verify all Finding fields are set correctly from clippy output."""
        output = _clippy_message(
            "unwrap-used", "src/main.rs", 42, "called `.unwrap()` on an `Option`"
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.repo == str(tmp_path)
        assert f.path == "src/main.rs"
        assert f.line == 42
        assert f.rule == "clippy-unwrap-used"
        assert "unwrap" in f.snippet
        assert f.confidence == 0.85
        assert f.quick_win is False
        assert f.safe_to_autofix is False
        assert f.severity == "medium"
        assert f.category == "lint"

    def test_snippet_truncated_at_200_chars(self, plugin, tmp_path) -> None:
        """Snippet should be truncated to 200 chars."""
        long_rendered = "x" * 500
        output = _clippy_message("panic", "src/lib.rs", 1, long_rendered)
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings[0].snippet) == 200


class TestScanTodosBehavior:
    """Behavior tests for TODO/FIXME scanning with real .rs files."""

    def test_multiple_files_nested_dirs(self, plugin, tmp_path) -> None:
        """Should find TODOs across nested directories."""
        (tmp_path / "src" / "network").mkdir(parents=True)
        (tmp_path / "src" / "main.rs").write_text(
            "fn main() {\n    // TODO: implement networking\n}\n"
        )
        (tmp_path / "src" / "network" / "conn.rs").write_text(
            "// FIXME: handle timeout\nfn connect() {}\n"
        )
        (tmp_path / "build.rs").write_text("// TODO: custom build\nfn main() {}\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 3
        rules = [f.rule for f in findings]
        assert all(r == "debt-todo-marker" for r in rules)

    def test_todo_line_numbers_accurate(self, plugin, tmp_path) -> None:
        """Line numbers in findings should match actual file positions."""
        content = "fn foo() {}\n\nfn bar() {\n    // TODO: fix this\n}\n"
        (tmp_path / "lib.rs").write_text(content)
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 1
        assert findings[0].line == 4

    def test_todo_and_fixme_in_same_file(self, plugin, tmp_path) -> None:
        """Both TODO and FIXME in same file produce separate findings."""
        (tmp_path / "main.rs").write_text(
            "// TODO: first\nfn main() {}\n// FIXME: second\n"
        )
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 2
        snippets = [f.snippet for f in findings]
        assert any("TODO" in s for s in snippets)
        assert any("FIXME" in s for s in snippets)

    def test_clean_file_no_findings(self, plugin, tmp_path) -> None:
        """Clean .rs file with no TODO/FIXME produces no findings."""
        (tmp_path / "clean.rs").write_text('fn main() { println!("hello"); }\n')
        findings = plugin._scan_todos(tmp_path)
        assert findings == []

    def test_todo_finding_field_values(self, plugin, tmp_path) -> None:
        """Verify all Finding fields for TODO markers."""
        (tmp_path / "main.rs").write_text("// TODO: implement later\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.repo == str(tmp_path)
        assert f.path == "main.rs"
        assert f.line == 1
        assert f.rule == "debt-todo-marker"
        assert f.confidence == 0.72
        assert f.severity == "low"
        assert f.category == "technical-debt"
        assert f.quick_win is False
        assert f.safe_to_autofix is False


class TestDiscoverEndToEnd:
    """End-to-end discover() with realistic scenarios."""

    def test_clippy_multiple_diagnostics_across_files(self, plugin, tmp_path) -> None:
        """Realistic clippy output with findings in multiple files."""
        output = "\n".join(
            [
                _artifact_line(),
                _clippy_message("unwrap-used", "src/main.rs", 15, "called `.unwrap()`"),
                _artifact_line(),
                _clippy_message(
                    "clone-on-copy",
                    "src/utils.rs",
                    88,
                    "using `clone` on a `Copy` type",
                ),
                _clippy_message("panic", "src/main.rs", 30, "this could panic"),
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=output):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 3
        paths = {f.path for f in findings}
        assert "src/main.rs" in paths
        assert "src/utils.rs" in paths

    def test_fallback_produces_correct_path_relative(self, plugin, tmp_path) -> None:
        """Fallback TODO scan produces relative paths."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("// TODO: implement me\n")
        with patch.object(plugin, "_run_tool", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1
        assert findings[0].path == "src/lib.rs"

    def test_code_without_clippy_prefix_still_parsed(self, plugin, tmp_path) -> None:
        """A non-clippy compiler message with code should still produce a finding."""
        output = json.dumps(
            {
                "reason": "compiler-message",
                "message": {
                    "code": {"code": "unused_variables"},
                    "spans": [{"file_name": "main.rs", "line_start": 5}],
                    "rendered": "unused variable",
                },
            }
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == "clippy-unused_variables"

    def test_code_obj_is_string_not_dict(self, plugin, tmp_path) -> None:
        """If message.code is a string instead of dict, should be handled."""
        output = json.dumps(
            {
                "reason": "compiler-message",
                "message": {
                    "code": "some_string",
                    "spans": [{"file_name": "main.rs", "line_start": 1}],
                    "rendered": "msg",
                },
            }
        )
        findings = plugin.parse_output(output, tmp_path)
        # code_obj is a string, not dict → code_obj.get('code', '') fails → empty code → skipped
        assert findings == []

    def test_empty_output_returns_empty(self, plugin, tmp_path) -> None:
        """Empty string output produces no findings."""
        findings = plugin.parse_output("", tmp_path)
        assert findings == []

    def test_finding_id_stable_across_calls(self, plugin, tmp_path) -> None:
        """Same input should produce same finding_id across two calls."""
        output = _clippy_message("unwrap-used", "src/main.rs", 10, "called `.unwrap()`")
        f1 = plugin.parse_output(output, tmp_path)
        f2 = plugin.parse_output(output, tmp_path)
        assert f1[0].finding_id == f2[0].finding_id
