"""Tests for plugins/dockerfile/plugin.py — Dockerfile discovery plugin."""

import hashlib
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def plugin():
    from plugins.dockerfile.plugin import Plugin

    return Plugin()


# ---------------------------------------------------------------------------
# parse_output
# ---------------------------------------------------------------------------


class TestParseOutput:
    def test_valid_output(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "code": "DL3006",
                    "line": 1,
                    "message": "untagged image",
                    "level": "warning",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == "docker-DL3006"

    def test_empty_code_uses_default(self, plugin, tmp_path):
        output = json.dumps(
            [{"code": "", "line": 1, "message": "m", "level": "warning"}]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].rule == "docker-DL0000"

    def test_error_level_confidence(self, plugin, tmp_path):
        output = json.dumps(
            [{"code": "DL3006", "line": 1, "message": "m", "level": "error"}]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].confidence == 0.85

    def test_warning_level_confidence(self, plugin, tmp_path):
        output = json.dumps(
            [{"code": "DL3006", "line": 1, "message": "m", "level": "warning"}]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].confidence == 0.78

    def test_invalid_json_returns_empty(self, plugin, tmp_path):
        assert plugin.parse_output("bad", tmp_path) == []

    def test_non_dict_items_skipped(self, plugin, tmp_path):
        output = json.dumps(
            [42, {"code": "DL3006", "line": 1, "message": "m", "level": "error"}]
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
        with patch("shutil.which", return_value="/usr/bin/hadolint"):
            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="hadolint", timeout=120),
            ):
                result = plugin._run_tool(tmp_path)
                assert result is None


# ---------------------------------------------------------------------------
# discover (regex fallback + tool integration)
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_regex_untagged_from(self, plugin, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubuntu\nRUN apt-get update\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "docker-DL3006" for f in findings)

    def test_regex_tagged_from_ok(self, plugin, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubuntu:22.04\nRUN echo hi\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "docker-DL3006" for f in findings)

    def test_regex_user_root(self, plugin, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM alpine\nUSER root\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "docker-DL3002" for f in findings)

    def test_regex_unpinned_apt(self, plugin, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM alpine\nRUN apt-get install curl\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "docker-DL3008" for f in findings)

    def test_regex_pinned_apt_ok(self, plugin, tmp_path):
        (tmp_path / "Dockerfile").write_text(
            "FROM alpine\nRUN apt-get install curl=7.0\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "docker-DL3008" for f in findings)

    def test_no_dockerfile(self, plugin, tmp_path):
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_containerfile(self, plugin, tmp_path):
        (tmp_path / "Containerfile").write_text("FROM ubuntu\nUSER root\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "docker-DL3002" for f in findings)

    def test_with_tool_output(self, plugin, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM ubuntu\n")
        mock_output = json.dumps(
            [{"code": "DL3006", "line": 1, "message": "untagged", "level": "warning"}]
        )
        with patch("shutil.which", return_value="/usr/bin/hadolint"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout=mock_output)
                findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# Plugin properties + stable_finding_id
# ---------------------------------------------------------------------------


class TestPluginProperties:
    def test_id_name_languages_rules(self, plugin):
        assert plugin.id == "plugin-dockerfile"
        assert plugin.name == "Dockerfile Plugin"
        assert plugin.languages == ["dockerfile"]
        assert "docker-DL3006" in plugin.rules


class TestStableFindingId:
    def test_deterministic_sha256(self):
        from plugins.dockerfile.plugin import stable_finding_id

        result = stable_finding_id("r", "Dockerfile", 1, "docker-DL3006", "FROM ubuntu")
        expected = hashlib.sha256(
            "r|Dockerfile|1|docker-DL3006|FROM ubuntu".encode("utf-8")
        ).hexdigest()
        assert result == expected


# ---------------------------------------------------------------------------
# Behavior-focused additions: multi-issue, field values, edge cases
# ---------------------------------------------------------------------------


class TestDiscoverMultipleIssues:
    """discover() with Dockerfiles containing multiple anti-patterns."""

    def test_all_three_issues_in_one_dockerfile(self, plugin, tmp_path):
        """Untagged FROM, USER root, and unpinned apt-get all flagged."""
        (tmp_path / "Dockerfile").write_text(
            "FROM ubuntu\nUSER root\nRUN apt-get install curl\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        rules = {f.rule for f in findings}
        assert "docker-DL3006" in rules
        assert "docker-DL3002" in rules
        assert "docker-DL3008" in rules

    def test_line_numbers_correct_for_each_issue(self, plugin, tmp_path):
        """Each finding should have the correct line number."""
        (tmp_path / "Dockerfile").write_text(
            "FROM ubuntu\nRUN echo hello\nUSER root\nRUN apt-get install wget\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        by_rule = {f.rule: f.line for f in findings}
        assert by_rule["docker-DL3006"] == 1
        assert by_rule["docker-DL3002"] == 3
        assert by_rule["docker-DL3008"] == 4

    def test_containerfile_all_issues(self, plugin, tmp_path):
        """Containerfile should be scanned the same as Dockerfile."""
        (tmp_path / "Containerfile").write_text(
            "FROM alpine\nUSER root\nRUN apt-get install vim\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        rules = {f.rule for f in findings}
        assert "docker-DL3006" in rules
        assert "docker-DL3002" in rules
        assert "docker-DL3008" in rules

    def test_clean_dockerfile_no_findings(self, plugin, tmp_path):
        """A well-formed Dockerfile should produce zero findings."""
        (tmp_path / "Dockerfile").write_text(
            "FROM ubuntu:22.04\n"
            "RUN apt-get update && apt-get install curl=7.88.1\n"
            "USER app\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []


class TestDiscoverFieldValues:
    """Verify Finding field values from regex-based discovery."""

    @pytest.mark.parametrize(
        "content_line,rule,expected_confidence,expected_category",
        [
            ("FROM ubuntu\n", "docker-DL3006", 0.85, "security"),
            ("USER root\n", "docker-DL3002", 0.82, "security"),
            ("RUN apt-get install curl\n", "docker-DL3008", 0.80, "security"),
        ],
    )
    def test_regex_finding_fields(
        self,
        plugin,
        tmp_path,
        content_line,
        rule,
        expected_confidence,
        expected_category,
    ):
        """Each regex rule should have correct confidence and category."""
        (tmp_path / "Dockerfile").write_text(content_line)
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        matching = [f for f in findings if f.rule == rule]
        assert len(matching) == 1
        f = matching[0]
        assert f.confidence == expected_confidence
        assert f.category == expected_category
        assert f.severity == "medium"
        assert f.quick_win is False
        assert f.safe_to_autofix is False
        assert f.path == "Dockerfile"

    def test_path_is_containerfile_for_containerfile(self, plugin, tmp_path):
        """Findings from Containerfile should have path='Containerfile'."""
        (tmp_path / "Containerfile").write_text("FROM busybox\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1
        assert findings[0].path == "Containerfile"


class TestParseOutputMultipleFindings:
    """Verify parse_output with realistic multi-finding hadolint JSON."""

    def test_realistic_hadolint_output(self, plugin, tmp_path):
        """Real hadolint output with multiple diagnostics."""
        output = json.dumps(
            [
                {
                    "code": "DL3006",
                    "line": 1,
                    "message": "Always tag the version of an image",
                    "level": "warning",
                },
                {
                    "code": "DL3002",
                    "line": 3,
                    "message": "Last USER should not be root",
                    "level": "warning",
                },
                {
                    "code": "DL3008",
                    "line": 5,
                    "message": "Pin versions in apt get install",
                    "level": "info",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 3
        for f in findings:
            assert f.path == "Dockerfile"
            assert f.severity == "medium"
            assert f.category == "security"

    def test_all_fields_from_tool(self, plugin, tmp_path):
        """Verify all fields populated from hadolint output."""
        output = json.dumps(
            [
                {
                    "code": "DL3006",
                    "line": 7,
                    "message": "untagged image",
                    "level": "error",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        f = findings[0]
        assert f.rule == "docker-DL3006"
        assert f.line == 7
        assert f.snippet == "untagged image"
        assert f.confidence == 0.85  # error level
        assert f.path == "Dockerfile"

    def test_mixed_error_and_warning_levels(self, plugin, tmp_path):
        """Error level gets confidence=0.85, warning gets 0.78."""
        output = json.dumps(
            [
                {"code": "DL3006", "line": 1, "message": "m1", "level": "error"},
                {"code": "DL3002", "line": 2, "message": "m2", "level": "warning"},
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        by_rule = {f.rule: f.confidence for f in findings}
        assert by_rule["docker-DL3006"] == 0.85
        assert by_rule["docker-DL3002"] == 0.78


class TestDiscoverEdgeCases:
    """Edge cases for Dockerfile discovery."""

    def test_from_with_as_keyword(self, plugin, tmp_path):
        """FROM image AS builder should not flag untagged (has no ':')."""
        (tmp_path / "Dockerfile").write_text("FROM rust AS builder\nRUN cargo build\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "docker-DL3006" for f in findings)

    def test_from_scratch_not_flagged_for_tag(self, plugin, tmp_path):
        """FROM scratch has no colon, so it IS flagged by DL3006 regex."""
        (tmp_path / "Dockerfile").write_text("FROM scratch\nCOPY binary /\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "docker-DL3006" for f in findings)

    def test_both_dockerfile_and_containerfile_exist(self, plugin, tmp_path):
        """Both Dockerfile and Containerfile should be scanned."""
        (tmp_path / "Dockerfile").write_text("FROM alpine\nUSER root\n")
        (tmp_path / "Containerfile").write_text("FROM ubuntu\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        paths = {f.path for f in findings}
        assert "Dockerfile" in paths
        assert "Containerfile" in paths
