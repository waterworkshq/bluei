"""Tests for plugins/shell/plugin.py — Shell discovery plugin."""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def plugin():
    from plugins.shell.plugin import Plugin

    return Plugin()


# ---------------------------------------------------------------------------
# parse_output
# ---------------------------------------------------------------------------


class TestParseOutput:
    def test_valid_output(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "code": "2086",
                    "line": 5,
                    "file": "script.sh",
                    "message": "quote var",
                    "level": "error",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1
        assert findings[0].rule == "shell-SC2086"
        assert findings[0].confidence == 0.88

    def test_warning_level_confidence(self, plugin, tmp_path):
        output = json.dumps(
            [
                {
                    "code": "2004",
                    "line": 1,
                    "file": "a.sh",
                    "message": "old syntax",
                    "level": "warning",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].confidence == 0.80

    def test_invalid_json_returns_empty(self, plugin, tmp_path):
        assert plugin.parse_output("bad", tmp_path) == []

    def test_non_list_returns_empty(self, plugin, tmp_path):
        assert plugin.parse_output("{}", tmp_path) == []

    def test_non_dict_items_skipped(self, plugin, tmp_path):
        output = json.dumps(
            [
                42,
                {
                    "code": "2086",
                    "line": 1,
                    "file": "x.sh",
                    "message": "m",
                    "level": "error",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# _run_tool
# ---------------------------------------------------------------------------


class TestRunTool:
    def test_accepts_rc1(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("echo hi\n")
        with patch("shutil.which", return_value="/usr/bin/shellcheck"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stdout='[{"code":"2086","line":1,"file":"test.sh","message":"m","level":"error"}]',
                )
                result = plugin._run_tool(tmp_path, extra_args=["test.sh"])
                assert result is not None

    def test_rejects_rc2(self, plugin, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/shellcheck"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=2, stdout="error")
                result = plugin._run_tool(tmp_path)
                assert result is None

    def test_empty_stdout_returns_none(self, plugin, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/shellcheck"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="")
                result = plugin._run_tool(tmp_path)
                assert result is None

    def test_timeout_returns_none(self, plugin, tmp_path):
        with patch("shutil.which", return_value="/usr/bin/shellcheck"):
            with patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="shellcheck", timeout=120),
            ):
                result = plugin._run_tool(tmp_path)
                assert result is None

    def test_not_installed_returns_none(self, plugin, tmp_path):
        with patch("shutil.which", return_value=None):
            result = plugin._run_tool(tmp_path)
            assert result is None


# ---------------------------------------------------------------------------
# discover (regex fallback + tool integration)
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_regex_unquoted_var(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("echo $FOO\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "shell-SC2086" for f in findings)

    def test_regex_unsafe_cd(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("cd /tmp/something\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "shell-SC2164" for f in findings)

    def test_regex_deprecated_arithmetic(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("echo $[1+2]\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "shell-SC2004" for f in findings)

    def test_safe_cd_not_flagged(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("cd /tmp && echo ok\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "shell-SC2164" for f in findings)

    def test_no_shell_files(self, plugin, tmp_path):
        (tmp_path / "readme.txt").write_text("hello")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_with_tool_output(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("echo $FOO\n")
        mock_output = json.dumps(
            [
                {
                    "code": "2086",
                    "line": 1,
                    "file": "test.sh",
                    "message": "quote",
                    "level": "error",
                }
            ]
        )
        with patch("shutil.which", return_value="/usr/bin/shellcheck"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout=mock_output)
                findings = plugin.discover(tmp_path, {})
        assert len(findings) == 1
        assert findings[0].rule == "shell-SC2086"

    def test_oserror_on_read(self, plugin, tmp_path):
        (tmp_path / "test.sh").write_text("echo hi\n")
        with patch("shutil.which", return_value=None):
            with patch.object(Path, "read_text", side_effect=OSError("nope")):
                findings = plugin.discover(tmp_path, {})
        assert findings == []

    def test_scripts_dir(self, plugin, tmp_path):
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("echo $PATH\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) > 0

    def test_bin_dir(self, plugin, tmp_path):
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "run.sh").write_text("cd /opt/app\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert any(f.rule == "shell-SC2164" for f in findings)


# ---------------------------------------------------------------------------
# Plugin properties + stable_finding_id
# ---------------------------------------------------------------------------


class TestPluginProperties:
    def test_id_name_languages_rules(self, plugin):
        assert plugin.id == "plugin-shell"
        assert plugin.name == "Shell Plugin"
        assert plugin.languages == ["shell"]
        assert "shell-SC2086" in plugin.rules


class TestStableFindingId:
    def test_deterministic_sha256(self):
        from plugins.shell.plugin import stable_finding_id

        result = stable_finding_id("r", "s.sh", 5, "shell-SC2086", "echo $FOO")
        expected = hashlib.sha256(
            "r|s.sh|5|shell-SC2086|echo $FOO".encode("utf-8")
        ).hexdigest()
        assert result == expected


# ---------------------------------------------------------------------------
# Behavior-focused additions: multi-issue, field values, false-positive checks
# ---------------------------------------------------------------------------


class TestDiscoverMultipleIssues:
    """discover() with shell scripts containing multiple anti-patterns."""

    def test_all_three_issues_in_one_script(self, plugin, tmp_path):
        """All three regex patterns flagged in a single file."""
        (tmp_path / "build.sh").write_text(
            "#!/bin/bash\ncd /opt/project\necho $HOME\nRESULT=$[1+2]\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        rules = {f.rule for f in findings}
        assert "shell-SC2164" in rules  # unsafe cd
        assert "shell-SC2086" in rules  # unquoted var
        assert "shell-SC2004" in rules  # deprecated arithmetic

    def test_line_numbers_accurate(self, plugin, tmp_path):
        """Each finding should have the correct line number."""
        (tmp_path / "test.sh").write_text(
            "#!/bin/bash\necho hello\ncd /opt/app\necho $VAR\nVAL=$[3+4]\n"
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        by_rule = {f.rule: f.line for f in findings}
        assert by_rule["shell-SC2164"] == 3
        assert by_rule["shell-SC2086"] == 4
        assert by_rule["shell-SC2004"] == 5

    def test_multiple_files_in_different_dirs(self, plugin, tmp_path):
        """Scripts in root, scripts/, and bin/ all scanned."""
        (tmp_path / "root.sh").write_text("echo $PATH\n")
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "build.sh").write_text("cd /opt\n")
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "run.sh").write_text("echo $[1+1]\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert len(findings) == 3
        paths = {f.path for f in findings}
        assert "root.sh" in paths
        assert "scripts/build.sh" in paths
        assert "bin/run.sh" in paths

    def test_clean_script_no_findings(self, plugin, tmp_path):
        """A clean shell script with no anti-patterns produces no findings."""
        (tmp_path / "clean.sh").write_text(
            '#!/bin/bash\nset -euo pipefail\necho "hello"\ncd /tmp && echo ok\n'
        )
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert findings == []


class TestDiscoverFalsePositiveGuards:
    """Verify regex patterns don't flag safe code."""

    def test_quoted_variable_not_flagged(self, plugin, tmp_path):
        """echo "$FOO" should NOT be flagged for unquoted variable."""
        (tmp_path / "safe.sh").write_text('echo "$FOO"\n')
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "shell-SC2086" for f in findings)

    def test_cd_with_or_not_flagged(self, plugin, tmp_path):
        """cd /tmp || exit 1 should NOT be flagged for unsafe cd."""
        (tmp_path / "safe.sh").write_text("cd /opt || exit 1\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "shell-SC2164" for f in findings)

    def test_cd_with_and_not_flagged(self, plugin, tmp_path):
        """cd /tmp && echo ok should NOT be flagged for unsafe cd."""
        (tmp_path / "safe.sh").write_text("cd /opt && echo ok\n")
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "shell-SC2164" for f in findings)

    def test_assignment_with_dollar_not_flagged(self, plugin, tmp_path):
        """VAR=$HOME in quotes is safe, but VAR=$HOME without quotes triggers SC2086."""
        (tmp_path / "test.sh").write_text('DIR="$HOME"\n')
        with patch("shutil.which", return_value=None):
            findings = plugin.discover(tmp_path, {})
        assert not any(f.rule == "shell-SC2086" for f in findings)


class TestDiscoverFieldValues:
    """Verify Finding field values from regex-based discovery."""

    @pytest.mark.parametrize(
        "content,rule,expected_confidence,expected_category",
        [
            ("echo $FOO\n", "shell-SC2086", 0.88, "bug"),
            ("cd /opt\n", "shell-SC2164", 0.85, "bug"),
            ("echo $[1+2]\n", "shell-SC2004", 0.90, "lint"),
        ],
    )
    def test_regex_finding_fields(
        self, plugin, tmp_path, content, rule, expected_confidence, expected_category
    ):
        """Each regex rule should have correct confidence and category."""
        (tmp_path / "test.sh").write_text(content)
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
        assert f.path == "test.sh"
        assert f.line == 1


class TestParseOutputCategoryMapping:
    """Verify parse_output maps shellcheck level to correct category."""

    def test_error_level_gives_bug_category(self, plugin, tmp_path):
        """Error-level shellcheck diagnostics should have category='bug'."""
        output = json.dumps(
            [
                {
                    "code": "2086",
                    "line": 1,
                    "file": "a.sh",
                    "message": "quote var",
                    "level": "error",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].category == "bug"
        assert findings[0].confidence == 0.88

    def test_warning_level_gives_lint_category(self, plugin, tmp_path):
        """Warning-level shellcheck diagnostics should have category='lint'."""
        output = json.dumps(
            [
                {
                    "code": "2004",
                    "line": 1,
                    "file": "a.sh",
                    "message": "old math",
                    "level": "warning",
                }
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert findings[0].category == "lint"
        assert findings[0].confidence == 0.80

    def test_multiple_findings_from_tool(self, plugin, tmp_path):
        """Realistic shellcheck output with multiple findings."""
        output = json.dumps(
            [
                {
                    "code": "2086",
                    "line": 3,
                    "file": "deploy.sh",
                    "message": "Double quote",
                    "level": "error",
                },
                {
                    "code": "2164",
                    "line": 7,
                    "file": "deploy.sh",
                    "message": "Use cd ... || exit",
                    "level": "error",
                },
                {
                    "code": "2004",
                    "line": 12,
                    "file": "deploy.sh",
                    "message": "Use $((..))",
                    "level": "warning",
                },
            ]
        )
        findings = plugin.parse_output(output, tmp_path)
        assert len(findings) == 3
        by_rule = {f.rule: f for f in findings}
        assert "shell-SC2086" in by_rule
        assert "shell-SC2164" in by_rule
        assert "shell-SC2004" in by_rule
        assert by_rule["shell-SC2086"].line == 3
        assert by_rule["shell-SC2164"].line == 7
        assert by_rule["shell-SC2004"].line == 12


class TestDiscoverWithToolOutput:
    """Verify discover() uses tool output when available and skips regex."""

    def test_tool_output_supersedes_regex(self, plugin, tmp_path):
        """When tool returns output, regex fallback should not run."""
        (tmp_path / "test.sh").write_text("echo $FOO\ncd /opt\n")
        mock_output = json.dumps(
            [
                {
                    "code": "2086",
                    "line": 1,
                    "file": "test.sh",
                    "message": "quote",
                    "level": "error",
                }
            ]
        )
        with patch("shutil.which", return_value="/usr/bin/shellcheck"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout=mock_output)
                findings = plugin.discover(tmp_path, {})
        # Only tool findings, no regex findings
        assert len(findings) == 1
        assert findings[0].rule == "shell-SC2086"
