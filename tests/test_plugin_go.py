"""Tests for plugins.go.plugin — Go static analysis and TODO scanning."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from plugins.go.plugin import Plugin, stable_finding_id


@pytest.fixture
def plugin():
    """Create a Go Plugin with mocked manifest loading."""
    with patch(
        "plugins.go.plugin.yaml.safe_load",
        return_value={
            "discovery": {"tool": "staticcheck", "tool_args": ["-f=json", "./..."]}
        },
    ):
        return Plugin()


class TestStableFindingId:
    def test_deterministic(self):
        id1 = stable_finding_id("repo", "main.go", 10, "rule", "msg")
        id2 = stable_finding_id("repo", "main.go", 10, "rule", "msg")
        assert id1 == id2
        assert len(id1) == 64

    def test_different_inputs(self):
        id1 = stable_finding_id("repo", "main.go", 10, "rule", "msg1")
        id2 = stable_finding_id("repo", "main.go", 10, "rule", "msg2")
        assert id1 != id2

    def test_whitespace_stripped(self):
        id1 = stable_finding_id("repo", "main.go", 10, "rule", "  msg  ")
        id2 = stable_finding_id("repo", "main.go", 10, "rule", "msg")
        assert id1 == id2


class TestGoPluginProperties:
    def test_id(self, plugin):
        assert plugin.id == "plugin-go"

    def test_name(self, plugin):
        assert plugin.name == "Go Plugin"

    def test_languages(self, plugin):
        assert plugin.languages == ["go"]

    def test_rules(self, plugin):
        assert "go-staticcheck-SA4006" in plugin.rules
        assert "debt-todo-marker" in plugin.rules


class TestGoPluginParseOutput:
    def test_single_finding(self, plugin):
        output = json.dumps(
            {
                "code": "SA4006",
                "location": {"file": "main.go", "line": 42},
                "message": "this value is never used",
            }
        )
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 1
        assert findings[0].rule == "go-staticcheck-SA4006"
        assert findings[0].line == 42
        assert findings[0].path == "main.go"
        assert findings[0].confidence == 0.85
        assert findings[0].severity == "medium"
        assert findings[0].category == "lint"

    def test_multiple_findings(self, plugin):
        lines = [
            json.dumps(
                {
                    "code": "SA4006",
                    "location": {"file": "a.go", "line": 1},
                    "message": "unused",
                }
            ),
            json.dumps(
                {
                    "code": "S1000",
                    "location": {"file": "b.go", "line": 2},
                    "message": "simplify",
                }
            ),
        ]
        output = "\n".join(lines)
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 2
        assert findings[0].rule == "go-staticcheck-SA4006"
        assert findings[1].rule == "go-staticcheck-S1000"

    def test_empty_code_skipped(self, plugin):
        output = json.dumps(
            {"code": "", "location": {"file": "a.go", "line": 1}, "message": "skip me"}
        )
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 0

    def test_missing_code_skipped(self, plugin):
        output = json.dumps(
            {"location": {"file": "a.go", "line": 1}, "message": "no code"}
        )
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 0

    def test_invalid_json_skipped(self, plugin):
        output = "NOT JSON\nALSO NOT JSON"
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 0

    def test_missing_location_defaults(self, plugin):
        output = json.dumps({"code": "SA4006", "message": "something"})
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 1
        assert findings[0].path == ""
        assert findings[0].line == 1

    def test_snippet_truncated(self, plugin):
        long_msg = "x" * 300
        output = json.dumps(
            {
                "code": "SA4006",
                "location": {"file": "a.go", "line": 1},
                "message": long_msg,
            }
        )
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings[0].snippet) == 200

    def test_empty_output(self, plugin):
        findings = plugin.parse_output("", Path("/repo"))
        assert findings == []

    def test_whitespace_only_output(self, plugin):
        findings = plugin.parse_output("   \n  \n  ", Path("/repo"))
        assert findings == []

    def test_mixed_valid_invalid(self, plugin):
        output = (
            "bad json\n"
            + json.dumps(
                {
                    "code": "S1000",
                    "location": {"file": "a.go", "line": 5},
                    "message": "ok",
                }
            )
            + "\nmore bad"
        )
        findings = plugin.parse_output(output, Path("/repo"))
        assert len(findings) == 1


class TestGoPluginScanTodos:
    def test_finds_todo(self, plugin, tmp_path):
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n// TODO: fix this\nfunc main() {}\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == "debt-todo-marker"
        assert findings[0].line == 2
        assert findings[0].severity == "low"
        assert findings[0].category == "technical-debt"
        assert findings[0].confidence == 0.72

    def test_finds_fixme(self, plugin, tmp_path):
        go_file = tmp_path / "util.go"
        go_file.write_text("package main\n// FIXME: broken\nfunc Foo() {}\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 1
        assert "FIXME" in findings[0].snippet

    def test_no_todos(self, plugin, tmp_path):
        go_file = tmp_path / "clean.go"
        go_file.write_text("package main\nfunc main() {}\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 0

    def test_multiple_files(self, plugin, tmp_path):
        (tmp_path / "a.go").write_text("package main\n// TODO: a\n")
        (tmp_path / "b.go").write_text("package main\n// FIXME: b\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 2

    def test_skips_non_go_files(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("# TODO: fix\n")
        (tmp_path / "main.py").write_text("# TODO: fix\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 0

    def test_nested_dirs(self, plugin, tmp_path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "handler.go").write_text("package pkg\n// TODO: handle edge case\n")
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 1

    def test_unreadable_file_skipped(self, plugin, tmp_path):
        go_file = tmp_path / "bad.go"
        go_file.write_text("package main\n")
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            findings = plugin._scan_todos(tmp_path)
        assert findings == []

    def test_multiple_todos_per_file(self, plugin, tmp_path):
        (tmp_path / "multi.go").write_text(
            "package main\n// TODO: first\n// TODO: second\n// FIXME: third\n"
        )
        findings = plugin._scan_todos(tmp_path)
        assert len(findings) == 3


class TestGoPluginDiscover:
    def test_discover_with_tool_output(self, plugin, tmp_path):
        json_output = json.dumps(
            {
                "code": "SA4006",
                "location": {"file": "main.go", "line": 10},
                "message": "unused",
            }
        )
        with patch.object(plugin, "_run_tool", return_value=json_output):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1
        assert findings[0].rule == "go-staticcheck-SA4006"

    def test_discover_falls_back_to_todos(self, plugin, tmp_path):
        (tmp_path / "main.go").write_text("package main\n// TODO: fix later\n")
        with patch.object(plugin, "_run_tool", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1
        assert findings[0].rule == "debt-todo-marker"

    def test_discover_no_tool_no_todos(self, plugin, tmp_path):
        with patch.object(plugin, "_run_tool", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_discover_multiple_tool_findings(self, plugin, tmp_path):
        output = "\n".join(
            [
                json.dumps(
                    {
                        "code": "SA4006",
                        "location": {"file": "a.go", "line": 1},
                        "message": "m1",
                    }
                ),
                json.dumps(
                    {
                        "code": "S1000",
                        "location": {"file": "b.go", "line": 2},
                        "message": "m2",
                    }
                ),
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=output):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 2
