"""Tests for plugins/python/plugin.py — Python discovery plugin."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def plugin():
    from plugins.python.plugin import Plugin

    return Plugin()


def _write_files(tmp_path, files):
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# ---------------------------------------------------------------------------
# parse_output
# ---------------------------------------------------------------------------


class TestParseOutput:
    def test_valid_json_maps_codes(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "code": "E501",
                    "location": {"row": 10},
                    "path": "foo.py",
                    "message": "line too long",
                },
                {
                    "code": "E722",
                    "location": {"row": 20},
                    "path": "bar.py",
                    "message": "bare except",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 2
        assert findings[0].rule == "trailing-whitespace"
        assert findings[1].rule == "broad-except"

    def test_unknown_code_uses_ruff_prefix(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "code": "F401",
                    "location": {"row": 1},
                    "path": "x.py",
                    "message": "unused import",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].rule == "ruff-F401"

    def test_empty_code_skipped(self, plugin, tmp_path):
        output = json.dumps([{"code": "", "message": "skip me"}])
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 0

    def test_invalid_json_returns_empty(self, plugin, tmp_path):
        assert plugin.parse_output("not json", tmp_path) == []

    def test_non_list_returns_empty(self, plugin, tmp_path):
        assert plugin.parse_output('{"a": 1}', tmp_path) == []

    def test_non_dict_items_skipped(self, plugin, tmp_path):
        output = json.dumps(
            [
                42,
                "string",
                {
                    "code": "E501",
                    "location": {"row": 1},
                    "path": "x.py",
                    "message": "m",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# _text_scan (regex fallback)
# ---------------------------------------------------------------------------


class TestTextScan:
    def test_broad_except(self, plugin, tmp_path):
        _write_files(
            tmp_path, {"setup.py": "", "test.py": "try:\n    pass\nexcept:\n    pass\n"}
        )
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "broad-except" for f in findings)

    def test_trailing_whitespace(self, plugin, tmp_path):
        _write_files(tmp_path, {"pyproject.toml": "", "test.py": "x = 1   \n"})
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "trailing-whitespace" for f in findings)

    def test_todo_marker(self, plugin, tmp_path):
        _write_files(tmp_path, {"setup.py": "", "test.py": "# TODO: fix this later\n"})
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "debt-todo-marker" for f in findings)

    def test_fixme_marker(self, plugin, tmp_path):
        _write_files(tmp_path, {"pyproject.toml": "", "test.py": "# FIXME: broken\n"})
        findings = plugin._text_scan(tmp_path)
        assert any(f.rule == "debt-todo-marker" for f in findings)

    def test_no_python_project_returns_empty(self, plugin, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        assert plugin._text_scan(tmp_path) == []

    def test_skips_pycache(self, plugin, tmp_path):
        _write_files(tmp_path, {"setup.py": ""})
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "test.cpython-311.pyc").write_text("")
        findings = plugin._text_scan(tmp_path)
        assert not any("__pycache__" in f.path for f in findings)

    def test_exception_during_file_read(self, plugin, tmp_path):
        _write_files(tmp_path, {"setup.py": "", "bad.py": "except:\n    pass\n"})
        with patch.object(Path, "read_text", side_effect=PermissionError("no")):
            findings = plugin._text_scan(tmp_path)
        assert findings == []


# ---------------------------------------------------------------------------
# discover (integration of tool + text)
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_merges_tool_and_text(self, plugin, tmp_path):
        _write_files(tmp_path, {"setup.py": "", "test.py": "# TODO: fix\n"})
        mock_output = json.dumps(
            [
                {
                    "code": "E501",
                    "location": {"row": 1},
                    "path": "test.py",
                    "message": "trailing",
                }
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=mock_output):
            findings = plugin.discover(tmp_path, {})
        rules = {f.rule for f in findings}
        assert "trailing-whitespace" in rules
        assert "debt-todo-marker" in rules

    def test_no_tool_falls_back_to_text_scan(self, plugin, tmp_path):
        _write_files(tmp_path, {"setup.py": "", "test.py": "except:\n    pass\n"})
        with patch.object(plugin, "_run_tool", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "broad-except" for f in findings)

    def test_dedupes_by_path_line_rule(self, plugin, tmp_path):
        _write_files(tmp_path, {"setup.py": "", "test.py": "except:\n    pass\n"})
        mock_output = json.dumps(
            [
                {
                    "code": "E722",
                    "location": {"row": 1},
                    "path": "test.py",
                    "message": "bare except",
                }
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=mock_output):
            findings = plugin.discover(tmp_path, {})
        broad_except = [f for f in findings if f.rule == "broad-except"]
        assert len(broad_except) == 1


# ---------------------------------------------------------------------------
# Plugin properties
# ---------------------------------------------------------------------------


class TestPluginProperties:
    def test_id_name_languages_rules(self, plugin):
        assert plugin.id == "plugin-python"
        assert plugin.name == "Python Plugin"
        assert plugin.languages == ["python"]
        assert len(plugin.rules) == 11


# ---------------------------------------------------------------------------
# stable_finding_id
# ---------------------------------------------------------------------------


class TestStableFindingId:
    def test_deterministic_sha256(self):
        from plugins.python.plugin import stable_finding_id

        result = stable_finding_id("repo", "path.py", 1, "rule", "snippet")
        expected = hashlib.sha256(
            "repo|path.py|1|rule|snippet".encode("utf-8")
        ).hexdigest()
        assert result == expected


# ---------------------------------------------------------------------------
# Behavior-focused additions: rule mapping, field values, edge cases
# ---------------------------------------------------------------------------


class TestRuffRuleMapping:
    """Verify all entries in _RUFF_RULE_MAP produce correct rule names."""

    @pytest.mark.parametrize(
        "ruff_code,expected_rule",
        [
            ("E501", "trailing-whitespace"),
            ("E722", "broad-except"),
            ("B001", "broad-except"),
            ("S108", "hardcoded-tmp-path"),
        ],
    )
    def test_mapped_codes(self, plugin, tmp_path, ruff_code, expected_rule):
        """Known ruff codes should map to internal rule names."""
        output = json.dumps(
            [
                {
                    "code": ruff_code,
                    "location": {"row": 1},
                    "path": "test.py",
                    "message": "msg",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == expected_rule

    @pytest.mark.parametrize(
        "unknown_code",
        ["F401", "W291", "C901", "N801", "SIM101"],
    )
    def test_unknown_codes_get_ruff_prefix(self, plugin, tmp_path, unknown_code):
        """Unknown ruff codes should become ruff-{code}."""
        output = json.dumps(
            [
                {
                    "code": unknown_code,
                    "location": {"row": 1},
                    "path": "x.py",
                    "message": "msg",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].rule == f"ruff-{unknown_code}"


class TestParseOutputQuickWinAutofix:
    """Verify quick_win and safe_to_autofix based on ruff code prefix."""

    @pytest.mark.parametrize("code", ["E501", "E722", "W291", "W391"])
    def test_e_w_codes_are_quick_win_and_autofix(self, plugin, tmp_path, code):
        """E and W codes should have quick_win=True and safe_to_autofix=True."""
        output = json.dumps(
            [{"code": code, "location": {"row": 1}, "path": "x.py", "message": "msg"}]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].quick_win is True
        assert findings[0].safe_to_autofix is True

    @pytest.mark.parametrize("code", ["F401", "S108", "B001", "C901"])
    def test_non_e_w_codes_are_not_quick_win(self, plugin, tmp_path, code):
        """Non-E/W codes should have quick_win=False and safe_to_autofix=False."""
        output = json.dumps(
            [{"code": code, "location": {"row": 1}, "path": "x.py", "message": "msg"}]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].quick_win is False
        assert findings[0].safe_to_autofix is False

    def test_all_fields_from_parse_output(self, plugin, tmp_path):
        """Verify all Finding fields are populated from ruff output."""
        output = json.dumps(
            [
                {
                    "code": "E501",
                    "location": {"row": 42},
                    "path": "src/app.py",
                    "message": "line too long",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        f = findings[0]
        assert f.repo == str(tmp_path)
        assert f.path == "src/app.py"
        assert f.line == 42
        assert f.rule == "trailing-whitespace"
        assert f.snippet == "line too long"
        assert f.confidence == 0.88
        assert f.severity == "medium"
        assert f.category == "lint"
        assert f.quick_win is True
        assert f.safe_to_autofix is True


class TestTextScanFieldValues:
    """Verify Finding field values from _text_scan."""

    def test_broad_except_fields(self, plugin, tmp_path):
        """Broad except should have correct confidence, severity, etc."""
        _write_files(
            tmp_path, {"setup.py": "", "test.py": "try:\n    pass\nexcept:\n    pass\n"}
        )
        findings = plugin._text_scan(tmp_path)
        broad = [f for f in findings if f.rule == "broad-except"]
        assert len(broad) == 1
        f = broad[0]
        assert f.confidence == 0.88
        assert f.quick_win is False
        assert f.safe_to_autofix is False
        assert f.severity == "medium"
        assert f.category == "lint"

    def test_trailing_whitespace_fields(self, plugin, tmp_path):
        """Trailing whitespace should have quick_win=True, safe_to_autofix=True."""
        _write_files(tmp_path, {"pyproject.toml": "", "test.py": "x = 1   \n"})
        findings = plugin._text_scan(tmp_path)
        tw = [f for f in findings if f.rule == "trailing-whitespace"]
        assert len(tw) == 1
        f = tw[0]
        assert f.confidence == 0.75
        assert f.quick_win is True
        assert f.safe_to_autofix is True
        assert f.severity == "low"
        assert f.category == "lint"

    def test_todo_marker_fields(self, plugin, tmp_path):
        """TODO marker should have correct fields."""
        _write_files(tmp_path, {"setup.py": "", "test.py": "# TODO: fix later\n"})
        findings = plugin._text_scan(tmp_path)
        todo = [f for f in findings if f.rule == "debt-todo-marker"]
        assert len(todo) == 1
        f = todo[0]
        assert f.confidence == 0.72
        assert f.quick_win is False
        assert f.safe_to_autofix is False
        assert f.severity == "low"
        assert f.category == "todo/debt"

    def test_trailing_whitespace_blank_line_not_flagged(self, plugin, tmp_path):
        """A blank line with only trailing spaces should NOT be flagged (strip() is falsy)."""
        _write_files(tmp_path, {"setup.py": "", "test.py": "   \n"})
        findings = plugin._text_scan(tmp_path)
        tw = [f for f in findings if f.rule == "trailing-whitespace"]
        assert len(tw) == 0

    def test_multiple_issues_in_single_file(self, plugin, tmp_path):
        """Single file with broad-except, trailing ws, and TODO produces 3 findings."""
        _write_files(
            tmp_path,
            {
                "setup.py": "",
                "app.py": "try:\n    pass\nexcept:\n    pass   \n# TODO: clean up\n",
            },
        )
        findings = plugin._text_scan(tmp_path)
        rules = {f.rule for f in findings}
        assert "broad-except" in rules
        assert "trailing-whitespace" in rules
        assert "debt-todo-marker" in rules

    def test_line_numbers_correct(self, plugin, tmp_path):
        """Findings should have correct line numbers."""
        _write_files(
            tmp_path,
            {
                "setup.py": "",
                "app.py": "x = 1\ntry:\n    pass\nexcept:\n    pass\n# TODO: later\n",
            },
        )
        findings = plugin._text_scan(tmp_path)
        by_rule = {f.rule: f.line for f in findings}
        assert by_rule["broad-except"] == 4
        assert by_rule["debt-todo-marker"] == 6


class TestDiscoverMergeBehavior:
    """Verify discover() correctly merges tool and text scan findings."""

    def test_non_overlapping_rules_merged(self, plugin, tmp_path):
        """Tool finds one rule, text scan finds another → both present."""
        _write_files(tmp_path, {"setup.py": "", "test.py": "# TODO: fix\n"})
        mock_output = json.dumps(
            [
                {
                    "code": "E501",
                    "location": {"row": 1},
                    "path": "test.py",
                    "message": "trailing",
                }
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=mock_output):
            findings = plugin.discover(tmp_path, {})
        rules = {f.rule for f in findings}
        assert "trailing-whitespace" in rules  # from tool
        assert "debt-todo-marker" in rules  # from text scan

    def test_overlapping_rules_deduped(self, plugin, tmp_path):
        """Same rule at same path/line from both sources → only one finding."""
        _write_files(tmp_path, {"setup.py": "", "test.py": "except:\n    pass\n"})
        mock_output = json.dumps(
            [
                {
                    "code": "E722",
                    "location": {"row": 1},
                    "path": "test.py",
                    "message": "bare except",
                }
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=mock_output):
            findings = plugin.discover(tmp_path, {})
        broad = [f for f in findings if f.rule == "broad-except"]
        assert len(broad) == 1

    def test_no_tool_no_text_project(self, plugin, tmp_path):
        """No tool, no setup.py/pyproject.toml → empty findings."""
        (tmp_path / "readme.txt").write_text("hello")
        with patch.object(plugin, "_run_tool", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_multiple_ruff_diagnostics_from_tool(self, plugin, tmp_path):
        """Realistic ruff output with multiple diagnostics."""
        _write_files(tmp_path, {"setup.py": ""})
        mock_output = json.dumps(
            [
                {
                    "code": "E501",
                    "location": {"row": 10},
                    "path": "app.py",
                    "message": "line too long",
                },
                {
                    "code": "E722",
                    "location": {"row": 20},
                    "path": "app.py",
                    "message": "bare except",
                },
                {
                    "code": "F401",
                    "location": {"row": 1},
                    "path": "app.py",
                    "message": "unused import",
                },
            ]
        )
        with patch.object(plugin, "_run_tool", return_value=mock_output):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 3
        rules = {f.rule for f in findings}
        assert "trailing-whitespace" in rules
        assert "broad-except" in rules
        assert "ruff-F401" in rules
