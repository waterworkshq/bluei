"""Tests for plugins/markdown/plugin.py — Markdown discovery plugin."""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def plugin():
    from plugins.markdown.plugin import Plugin

    return Plugin()


# ---------------------------------------------------------------------------
# parse_output
# ---------------------------------------------------------------------------


class TestParseOutput:
    def test_valid_output(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "ruleNames": ["MD009"],
                    "lineNumber": 5,
                    "ruleDescription": "trailing spaces",
                    "fileName": "readme.md",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == "MD009"

    def test_rule_fallback_from_rule_field(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "rule": "MD041",
                    "lineNumber": 1,
                    "ruleDescription": "first heading",
                    "fileName": "a.md",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].rule == "MD041"

    def test_empty_rule_names_uses_default(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "ruleNames": [],
                    "lineNumber": 1,
                    "ruleDescription": "x",
                    "fileName": "a.md",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].rule == "MD000"

    def test_invalid_json_returns_empty(self, plugin, tmp_path):
        assert plugin.parse_output("bad", tmp_path) == []

    def test_non_dict_items_skipped(self, plugin, tmp_path):
        output = json.dumps(
            [
                42,
                {
                    "ruleNames": ["MD009"],
                    "lineNumber": 1,
                    "ruleDescription": "x",
                    "fileName": "a.md",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# _run_tool
# ---------------------------------------------------------------------------


class TestRunTool:
    def test_not_installed_returns_none(self, plugin, tmp_path):
        with patch("shutil.which", return_value=None):
            result = plugin._run_tool(tmp_path)
            assert result is None

    def test_timeout_returns_none(self, plugin, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/markdownlint"):
            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="ml", timeout=120),
            ):
                result = plugin._run_tool(tmp_path)
                assert result is None


# ---------------------------------------------------------------------------
# discover (regex fallback + tool integration)
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_regex_missing_h1(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("This has no heading\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "md-MD041" for f in findings)

    def test_regex_has_h1_ok(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("# Title\n\nContent\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "md-MD041" for f in findings)

    def test_regex_trailing_spaces(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("# Title \n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "md-MD009" for f in findings)

    def test_no_trailing_spaces_ok(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("# Title\nClean line\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "md-MD009" for f in findings)

    def test_no_md_files(self, plugin, tmp_path):
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_docs_subdir(self, plugin, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("No heading here\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "md-MD041" for f in findings)

    def test_with_tool_output(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("# OK\n")
        mock_output = json.dumps(
            [
                {
                    "ruleNames": ["MD009"],
                    "lineNumber": 2,
                    "ruleDescription": "trailing",
                    "fileName": "readme.md",
                }
            ]
        )
        with patch("shutil.which", return_value="/usr/bin/markdownlint"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout=mock_output)
                findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1

    def test_oserror_on_read(self, plugin, tmp_path):
        (tmp_path / "readme.md").write_text("# OK\n")
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "read_text", side_effect=OSError("nope")):
                findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_empty_file_no_findings(self, plugin, tmp_path):
        (tmp_path / "empty.md").write_text("")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []


# ---------------------------------------------------------------------------
# Plugin properties + stable_finding_id
# ---------------------------------------------------------------------------


class TestPluginProperties:
    def test_id_name_languages_rules(self, plugin):
        assert plugin.id == "plugin-markdown"
        assert plugin.name == "Markdown Plugin"
        assert plugin.languages == ["markdown"]
        assert "md-MD009" in plugin.rules


class TestStableFindingId:
    def test_deterministic_sha256(self):
        from plugins.markdown.plugin import stable_finding_id

        result = stable_finding_id("r", "a.md", 3, "MD009", "trailing  ")
        expected = hashlib.sha256("r|a.md|3|MD009|trailing".encode("utf-8")).hexdigest()
        assert result == expected


# ---------------------------------------------------------------------------
# Behavior-focused additions: multi-file, field values, edge cases
# ---------------------------------------------------------------------------


class TestDiscoverMultipleFiles:
    """discover() with multiple markdown files containing various issues."""

    def test_two_files_different_issues(self, plugin, tmp_path):
        """Each file's issues should produce separate findings."""
        (tmp_path / "readme.md").write_text("No heading here\nClean line\n")
        (tmp_path / "guide.md").write_text("# Title\nHas trailing space   \n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        # readme.md: MD041; guide.md: MD009
        rules_by_path = {}
        for f in findings:
            rules_by_path.setdefault(f.path, set()).add(f.rule)
        assert "md-MD041" in rules_by_path.get("readme.md", set())
        assert "md-MD009" in rules_by_path.get("guide.md", set())

    def test_single_file_both_issues(self, plugin, tmp_path):
        """A file missing heading AND having trailing spaces gets both findings."""
        (tmp_path / "doc.md").write_text("No heading   \n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        rules = {f.rule for f in findings}
        assert "md-MD041" in rules
        assert "md-MD009" in rules

    def test_trailing_spaces_on_multiple_lines(self, plugin, tmp_path):
        """Each line with trailing spaces should produce a separate finding."""
        (tmp_path / "doc.md").write_text(
            "# Title\nLine one   \nLine two   \nClean line\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        md009 = [f for f in findings if f.rule == "md-MD009"]
        assert len(md009) == 2
        lines = {f.line for f in md009}
        assert lines == {2, 3}

    def test_docs_subdir_with_trailing_spaces(self, plugin, tmp_path):
        """Files in docs/ with trailing spaces are flagged."""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "api.md").write_text("# API\nTrailing   \n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "md-MD009" for f in findings)


class TestDiscoverFieldValues:
    """Verify Finding field values from regex-based discovery."""

    def test_md041_confidence_and_category(self, plugin, tmp_path):
        """MD041 (missing top heading) should have confidence=0.80, category='docs'."""
        (tmp_path / "readme.md").write_text("Just text\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        md041 = [f for f in findings if f.rule == "md-MD041"]
        assert len(md041) == 1
        f = md041[0]
        assert f.confidence == 0.80
        assert f.category == "docs"
        assert f.severity == "low"
        assert f.quick_win is False
        assert f.safe_to_autofix is False
        assert f.path == "readme.md"
        assert f.line == 1

    def test_md009_confidence_and_category(self, plugin, tmp_path):
        """MD009 (trailing spaces) should have confidence=0.90, category='lint'."""
        (tmp_path / "readme.md").write_text("# Title\nTrailing   \n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        md009 = [f for f in findings if f.rule == "md-MD009"]
        assert len(md009) == 1
        f = md009[0]
        assert f.confidence == 0.90
        assert f.category == "lint"
        assert f.severity == "low"

    def test_md041_only_on_line_1(self, plugin, tmp_path):
        """MD041 should always be on line 1 when the first line lacks a heading."""
        (tmp_path / "doc.md").write_text("Paragraph\n# Heading\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        md041 = [f for f in findings if f.rule == "md-MD041"]
        assert len(md041) == 1
        assert md041[0].line == 1


class TestParseOutputFieldValues:
    """Verify Finding field values from markdownlint parse_output."""

    def test_all_fields_from_tool_output(self, plugin, tmp_path):
        """All fields should be populated from markdownlint JSON output."""
        output = json.dumps(
            [
                {
                    "ruleNames": ["MD013", "MD001"],
                    "lineNumber": 42,
                    "ruleDescription": "Line length",
                    "fileName": "docs/guide.md",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        f = findings[0]
        assert f.rule == "MD013"
        assert f.path == "docs/guide.md"
        assert f.line == 42
        assert f.snippet == "Line length"
        assert f.confidence == 0.85
        assert f.severity == "low"
        assert f.category == "docs"
        assert f.quick_win is False
        assert f.safe_to_autofix is False

    @pytest.mark.parametrize(
        "output_field,expected_rule",
        [
            ("ruleNames", "MD041"),
            ("rule", "MD041"),
        ],
    )
    def test_rule_names_vs_rule_field(
        self, plugin, tmp_path, output_field, expected_rule
    ):
        """Should use ruleNames when available, fall back to rule."""
        if output_field == "ruleNames":
            item = {
                "ruleNames": ["MD041"],
                "lineNumber": 1,
                "ruleDescription": "x",
                "fileName": "a.md",
            }
        else:
            item = {
                "rule": "MD041",
                "lineNumber": 1,
                "ruleDescription": "x",
                "fileName": "a.md",
            }
        findings = plugin.parse_output(json.dumps([item]), tmp_path)
        assert findings[0].rule == expected_rule

    def test_rule_description_fallback_to_message(self, plugin, tmp_path):
        """When ruleDescription is absent, fall back to message field."""
        output = json.dumps(
            [
                {
                    "ruleNames": ["MD009"],
                    "lineNumber": 1,
                    "message": "trailing found",
                    "fileName": "a.md",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].snippet == "trailing found"

    def test_rule_names_as_string_uses_rule_field_fallback(self, plugin, tmp_path):
        """If ruleNames is a string instead of list, falls back to 'rule' field."""
        output = json.dumps(
            [
                {
                    "ruleNames": "MD009",
                    "lineNumber": 1,
                    "ruleDescription": "x",
                    "fileName": "a.md",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        # ruleNames is a string (not list), so falls back to item.get('rule', 'MD000')
        # which defaults to MD000 since 'rule' key is absent
        assert findings[0].rule == "MD000"


class TestDiscoverCleanFiles:
    """Verify clean files produce no findings."""

    def test_perfect_file_no_findings(self, plugin, tmp_path):
        """A well-formed markdown file should produce zero findings."""
        (tmp_path / "clean.md").write_text("# Title\n\nGood paragraph.\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_only_h1_is_not_flagged(self, plugin, tmp_path):
        """Only '# ' counts as valid top heading; '## ' does not."""
        (tmp_path / "doc.md").write_text("## Subtitle\n\nContent\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "md-MD041" for f in findings)
