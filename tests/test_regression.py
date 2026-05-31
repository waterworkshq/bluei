"""Tests for the regression detection module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.regression import (
    _find_export_changes,
    _find_lint_regressions,
    _find_test_deletions,
    _git_diff_names,
    _resolve_current_branch,
    check_regressions,
    compute_regression_score,
    detect_export_changes,
    detect_removed_tests,
)


class TestGitDiffNames:
    """git diff --name-status parsing."""

    def test_empty_output(self, tmp_path: Path) -> None:
        with patch("bluei.engine.regression.run_capture", return_value=(1, "")):
            result = _git_diff_names(tmp_path, "main", "feature")
            assert result == []

    def test_parses_changes(self, tmp_path: Path) -> None:
        output = "M\tsrc/file1.py\nA\tsrc/file2.py\nD\ttests/test_file.py\n"
        with patch("bluei.engine.regression.run_capture", return_value=(0, output)):
            result = _git_diff_names(tmp_path, "main", "feature")
            assert len(result) == 3
            assert result[0] == {"status": "M", "path": "src/file1.py"}
            assert result[1] == {"status": "A", "path": "src/file2.py"}
            assert result[2] == {"status": "D", "path": "tests/test_file.py"}


class TestFindTestDeletions:
    """Test file deletion detection."""

    def test_flags_deleted_test_file(self, tmp_path: Path) -> None:
        changes = [{"status": "D", "path": "tests/test_foo.py"}]
        with patch(
            "bluei.engine.regression.run_capture",
            return_value=(0, "def test_bar(): ...\ndef test_baz(): ...\n"),
        ):
            result = _find_test_deletions(changes, tmp_path, "main")
            assert len(result) == 1
            assert result[0]["type"] == "test_file_deleted"
            assert result[0]["path"] == "tests/test_foo.py"
            assert result[0]["estimated_functions"] == 2

    def test_ignores_non_test_deletions(self, tmp_path: Path) -> None:
        changes = [{"status": "D", "path": "src/foo.py"}]
        result = _find_test_deletions(changes, tmp_path, "main")
        assert result == []

    def test_ignores_additions_and_modifications(self, tmp_path: Path) -> None:
        changes = [
            {"status": "A", "path": "tests/test_new.py"},
            {"status": "M", "path": "tests/test_existing.py"},
        ]
        result = _find_test_deletions(changes, tmp_path, "main")
        assert result == []

    def test_detects_test_prefixes(self, tmp_path: Path) -> None:
        changes = [
            {"status": "D", "path": "test_foo.py"},
            {"status": "D", "path": "spec/bar_spec.rb"},
            {"status": "D", "path": "__tests__/baz.test.js"},
        ]
        with patch("bluei.engine.regression.run_capture", return_value=(0, "")):
            result = _find_test_deletions(changes, tmp_path, "main")
            assert len(result) == 3


class TestFindExportChanges:
    """Export surface change detection."""

    def test_flags_init_py_deletion(self) -> None:
        changes = [{"status": "D", "path": "mypackage/__init__.py"}]
        result = _find_export_changes(changes, Path("/tmp"))
        assert len(result) == 1
        assert result[0]["type"] == "module_init_deleted"

    def test_flags_index_ts_deletion(self) -> None:
        changes = [{"status": "D", "path": "src/index.ts"}]
        result = _find_export_changes(changes, Path("/tmp"))
        assert len(result) == 1
        assert result[0]["type"] == "export_module_deleted"

    def test_ignores_source_removal(self) -> None:
        changes = [{"status": "D", "path": "src/util.py"}]
        result = _find_export_changes(changes, Path("/tmp"))
        assert result == []


class TestCheckRegressions:
    """Full regression check integration."""

    def test_clean_diff_no_findings(self, tmp_path: Path) -> None:
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        with patch("bluei.engine.regression._git_diff_names", return_value=[]):
            result = check_regressions(tmp_path, "main", "feature", log_file)
            assert result == []

    def test_finds_test_deletion(self, tmp_path: Path) -> None:
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        mock_changes = [{"status": "D", "path": "tests/test_foo.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=mock_changes),
            patch(
                "bluei.engine.regression._find_test_deletions",
                return_value=[
                    {
                        "type": "test_file_deleted",
                        "path": "tests/test_foo.py",
                        "estimated_functions": 2,
                    }
                ],
            ),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            result = check_regressions(tmp_path, "main", "feature", log_file)
            assert len(result) == 1
            assert result[0]["type"] == "test_file_deleted"


class TestComputeRegressionScore:
    """Regression score computation."""

    def test_clean_score_is_zero(self, tmp_path: Path) -> None:
        from bluei.engine.regression import compute_regression_score

        with patch("bluei.engine.regression._git_diff_names", return_value=[]):
            score = compute_regression_score(tmp_path, "main")
            assert score == 0.0

    def test_nonexistent_path_returns_zero(self, tmp_path: Path) -> None:
        from bluei.engine.regression import compute_regression_score

        score = compute_regression_score(tmp_path / "nonexistent", "main")
        assert score == 0.0

    def test_test_deletion_adds_score(self, tmp_path: Path) -> None:
        from bluei.engine.regression import compute_regression_score

        mock_changes = [{"status": "D", "path": "tests/test_foo.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=mock_changes),
            patch(
                "bluei.engine.regression._find_test_deletions",
                return_value=[
                    {
                        "type": "test_file_deleted",
                        "path": "tests/test_foo.py",
                        "estimated_functions": 2,
                    }
                ],
            ),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            score = compute_regression_score(tmp_path, "main")
            # 1 test deletion * 0.15 = 0.15
            assert score == pytest.approx(0.15, rel=1e-3)

    def test_export_deletion_adds_score(self, tmp_path: Path) -> None:
        from bluei.engine.regression import compute_regression_score

        mock_changes = [{"status": "D", "path": "mypackage/__init__.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=mock_changes),
            patch("bluei.engine.regression._find_test_deletions", return_value=[]),
            patch(
                "bluei.engine.regression._find_export_changes",
                return_value=[
                    {"type": "module_init_deleted", "path": "mypackage/__init__.py"}
                ],
            ),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            score = compute_regression_score(tmp_path, "main")
            # init deletion: 0.2
            assert score == pytest.approx(0.2, rel=1e-3)

    def test_capped_at_one(self, tmp_path: Path) -> None:
        from bluei.engine.regression import compute_regression_score

        mock_changes = [
            {"status": "D", "path": "tests/t.py"},
            {"status": "D", "path": "exports/e.ts"},
        ]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=mock_changes),
            patch(
                "bluei.engine.regression._find_test_deletions",
                return_value=[{"type": "test_file_deleted"} for _ in range(10)],
            ),
            patch(
                "bluei.engine.regression._find_export_changes",
                return_value=[{"type": "export_module_deleted"} for _ in range(10)],
            ),
            patch(
                "bluei.engine.regression._find_lint_regressions",
                return_value=[{"type": "lint_regression"} for _ in range(10)],
            ),
        ):
            score = compute_regression_score(tmp_path, "main")
            assert score <= 1.0
            assert score == 1.0

    def test_imports_work(self) -> None:
        """Verify that all regression module symbols can be imported."""
        from bluei.engine.regression import (
            _git_diff_names,
            _find_test_deletions,
            _find_export_changes,
            _find_lint_regressions,
            check_regressions,
            compute_regression_score,
            detect_removed_tests,
            detect_export_changes,
            _resolve_current_branch,
        )

        assert callable(_git_diff_names)
        assert callable(compute_regression_score)


# ─── Appended from phase7b extraction ───
# Additional edge-case tests for existing classes, plus entirely new
# test classes for functions not previously covered.


class TestGitDiffNamesEdgeCases:
    """Additional git diff parsing edge cases."""

    def test_returns_empty_on_blank_output(self, tmp_path):
        with patch("bluei.engine.regression.run_capture", return_value=(0, "   \n")):
            assert _git_diff_names(tmp_path, "main", "feat") == []

    def test_skips_blank_lines(self, tmp_path):
        raw = "M\ta.py\n\nD\tb.py\n"
        with patch("bluei.engine.regression.run_capture", return_value=(0, raw)):
            result = _git_diff_names(tmp_path, "main", "feat")
            assert len(result) == 2

    def test_no_tab_line_skipped(self, tmp_path):
        raw = "?? untracked_file.py\nM\tsrc/good.py\n"
        with patch("bluei.engine.regression.run_capture", return_value=(0, raw)):
            result = _git_diff_names(tmp_path, "main", "feat")
            assert len(result) == 1

    def test_rename_status_with_two_tabs(self, tmp_path):
        raw = "R100\told_name.py\tnew_name.py\n"
        with patch("bluei.engine.regression.run_capture", return_value=(0, raw)):
            result = _git_diff_names(tmp_path, "main", "feat")
            assert len(result) == 1
            assert result[0]["status"] == "R100"


class TestFindTestDeletionsEdgeCases:
    """Additional test-deletion detection edge cases."""

    def test_git_show_failure_gives_zero_functions(self, tmp_path):
        changes = [{"status": "D", "path": "tests/test_foo.py"}]
        with patch("bluei.engine.regression.run_capture", return_value=(1, "")):
            result = _find_test_deletions(changes, tmp_path, "main")
            assert result[0]["estimated_functions"] == 0

    def test_counts_js_test_patterns(self, tmp_path):
        changes = [{"status": "D", "path": "__tests__/foo.test.js"}]
        content = (
            "test('works', () => {})\nit('does', () => {})\ndescribe('grp', () => {})\n"
        )
        with patch("bluei.engine.regression.run_capture", return_value=(0, content)):
            result = _find_test_deletions(changes, tmp_path, "main")
            assert result[0]["estimated_functions"] == 3

    def test_empty_git_show_content(self, tmp_path):
        changes = [{"status": "D", "path": "spec/bar_spec.rb"}]
        with patch("bluei.engine.regression.run_capture", return_value=(0, "")):
            result = _find_test_deletions(changes, tmp_path, "main")
            assert result[0]["estimated_functions"] == 0

    def test_multiple_deletions(self, tmp_path):
        changes = [
            {"status": "D", "path": "tests/a.py"},
            {"status": "D", "path": "test_b.py"},
            {"status": "M", "path": "tests/c.py"},
        ]
        with patch(
            "bluei.engine.regression.run_capture",
            return_value=(0, "def test_x(): pass"),
        ):
            result = _find_test_deletions(changes, tmp_path, "main")
            assert len(result) == 2


class TestFindExportChangesEdgeCases:
    """Additional export-change detection edge cases."""

    def test_nested_init_py(self):
        changes = [{"status": "D", "path": "pkg/sub/__init__.py"}]
        result = _find_export_changes(changes, Path("/tmp"))
        assert result[0]["type"] == "module_init_deleted"

    def test_exports_dir(self):
        changes = [{"status": "D", "path": "src/exports/api.ts"}]
        result = _find_export_changes(changes, Path("/tmp"))
        assert result[0]["type"] == "export_module_deleted"

    def test_index_js(self):
        changes = [{"status": "D", "path": "src/index.js"}]
        result = _find_export_changes(changes, Path("/tmp"))
        assert result[0]["type"] == "export_module_deleted"

    def test_non_deletion_status_ignored(self):
        changes = [
            {"status": "M", "path": "pkg/__init__.py"},
            {"status": "A", "path": "src/index.ts"},
        ]
        result = _find_export_changes(changes, Path("/tmp"))
        assert result == []


class TestFindLintRegressions:
    """Lint regression detection via ruff."""

    def test_no_ruff_config_returns_empty(self, tmp_path):
        result = _find_lint_regressions(tmp_path, "main", "feat")
        assert result == []

    def test_ruff_toml_present_no_diff(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("line-length = 88\n")
        with patch("bluei.engine.regression.run_capture", return_value=(1, "")):
            result = _find_lint_regressions(tmp_path, "main", "feat")
            assert result == []

    def test_ruff_toml_with_diff_but_no_py_files(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("line-length = 88\n")
        diff_output = "diff --git a/readme.md b/readme.md\n"
        with patch(
            "bluei.engine.regression.run_capture", return_value=(0, diff_output)
        ):
            result = _find_lint_regressions(tmp_path, "main", "feat")
            assert result == []

    def test_ruff_finds_violations(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("line-length = 88\n")
        diff_output = "+++ b/src/foo.py\n"
        ruff_output = "src/foo.py:10:5 F401 Unused import\n"
        calls = iter([(0, diff_output), (1, ruff_output)])
        with patch(
            "bluei.engine.regression.run_capture",
            side_effect=lambda *a, **kw: next(calls),
        ):
            result = _find_lint_regressions(tmp_path, "main", "feat")
            assert len(result) == 1
            assert result[0]["type"] == "lint_regression"
            assert result[0]["file"] == "src/foo.py"
            assert result[0]["line"] == 10

    def test_pyproject_ruff_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
        with patch("bluei.engine.regression.run_capture", return_value=(0, "")):
            result = _find_lint_regressions(tmp_path, "main", "feat")
            assert result == []

    def test_pyproject_without_ruff_section(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "foo"\n')
        result = _find_lint_regressions(tmp_path, "main", "feat")
        assert result == []

    def test_ruff_non_digit_line_handled(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("")
        diff_output = "+++ b/src/bar.py\n"
        ruff_output = "src/bar.py:abc:5 E501 line too long\n"
        calls = iter([(0, diff_output), (1, ruff_output)])
        with patch(
            "bluei.engine.regression.run_capture",
            side_effect=lambda *a, **kw: next(calls),
        ):
            result = _find_lint_regressions(tmp_path, "main", "feat")
            assert len(result) == 1
            assert result[0]["line"] == 0

    def test_ruff_output_with_four_parts(self, tmp_path):
        (tmp_path / "ruff.toml").write_text("")
        diff_output = "+++ b/x.py\n"
        ruff_output = "x.py:1:1:F401: some message here\n"
        calls = iter([(0, diff_output), (1, ruff_output)])
        with patch(
            "bluei.engine.regression.run_capture",
            side_effect=lambda *a, **kw: next(calls),
        ):
            result = _find_lint_regressions(tmp_path, "main", "feat")
            assert len(result) == 1
            assert result[0]["message"] == "F401: some message here"


class TestResolveCurrentBranch:
    """Git branch resolution."""

    def test_success(self, tmp_path):
        with patch(
            "bluei.engine.regression.run_capture", return_value=(0, "feature-branch\n")
        ):
            assert _resolve_current_branch(tmp_path) == "feature-branch"

    def test_failure_returns_head(self, tmp_path):
        with patch("bluei.engine.regression.run_capture", return_value=(1, "")):
            assert _resolve_current_branch(tmp_path) == "HEAD"

    def test_empty_output_returns_head(self, tmp_path):
        with patch("bluei.engine.regression.run_capture", return_value=(0, "   ")):
            assert _resolve_current_branch(tmp_path) == "HEAD"


class TestDetectRemovedTests:
    """High-level removed-test detection."""

    def test_nonexistent_path(self, tmp_path):
        assert detect_removed_tests(tmp_path / "nope", "main") == []

    def test_finds_deleted_tests(self, tmp_path):
        with (
            patch(
                "bluei.engine.regression._resolve_current_branch", return_value="feat"
            ),
            patch(
                "bluei.engine.regression._git_diff_names",
                return_value=[
                    {"status": "D", "path": "tests/test_x.py"},
                    {"status": "D", "path": "src/foo.py"},
                ],
            ),
        ):
            result = detect_removed_tests(tmp_path, "main")
            assert result == ["tests/test_x.py"]

    def test_no_deletions(self, tmp_path):
        with (
            patch(
                "bluei.engine.regression._resolve_current_branch", return_value="feat"
            ),
            patch(
                "bluei.engine.regression._git_diff_names",
                return_value=[{"status": "M", "path": "tests/test_y.py"}],
            ),
        ):
            assert detect_removed_tests(tmp_path, "main") == []


class TestDetectExportChanges:
    """High-level export change detection with human-readable output."""

    def test_nonexistent_path(self, tmp_path):
        assert detect_export_changes(tmp_path / "nope", "main") == []

    def test_init_deletion_message(self, tmp_path):
        with (
            patch(
                "bluei.engine.regression._resolve_current_branch", return_value="feat"
            ),
            patch(
                "bluei.engine.regression._git_diff_names",
                return_value=[{"status": "D", "path": "pkg/__init__.py"}],
            ),
        ):
            result = detect_export_changes(tmp_path, "main")
            assert "Init module deleted: pkg/__init__.py" in result

    def test_export_module_deletion_message(self, tmp_path):
        with (
            patch(
                "bluei.engine.regression._resolve_current_branch", return_value="feat"
            ),
            patch(
                "bluei.engine.regression._git_diff_names",
                return_value=[{"status": "D", "path": "src/index.ts"}],
            ),
        ):
            result = detect_export_changes(tmp_path, "main")
            assert "Export module deleted: src/index.ts" in result

    def test_unknown_type_fallback(self, tmp_path):
        with (
            patch(
                "bluei.engine.regression._resolve_current_branch", return_value="feat"
            ),
            patch("bluei.engine.regression._git_diff_names", return_value=[]),
            patch(
                "bluei.engine.regression._find_export_changes",
                return_value=[{"type": "unknown_thing", "path": "foo.py"}],
            ),
        ):
            result = detect_export_changes(tmp_path, "main")
            assert "Export change detected: foo.py" in result


class TestComputeRegressionScoreEdgeCases:
    """Additional regression score edge cases."""

    def test_multiple_test_dels_capped(self, tmp_path):
        changes = [{"status": "D", "path": "tests/a.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch(
                "bluei.engine.regression._find_test_deletions",
                return_value=[{"type": "test_file_deleted"} for _ in range(5)],
            ),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            score = compute_regression_score(tmp_path, "main")
            assert score == pytest.approx(0.3, rel=1e-3)

    def test_export_module_deletion_score(self, tmp_path):
        changes = [{"status": "D", "path": "src/index.ts"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch("bluei.engine.regression._find_test_deletions", return_value=[]),
            patch(
                "bluei.engine.regression._find_export_changes",
                return_value=[
                    {"type": "export_module_deleted", "path": "src/index.ts"}
                ],
            ),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            score = compute_regression_score(tmp_path, "main")
            assert score == pytest.approx(0.4, rel=1e-3)

    def test_lint_regressions_contribute(self, tmp_path):
        changes = [{"status": "M", "path": "x.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch("bluei.engine.regression._find_test_deletions", return_value=[]),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch(
                "bluei.engine.regression._find_lint_regressions",
                return_value=[{"type": "lint_regression"} for _ in range(2)],
            ),
        ):
            score = compute_regression_score(tmp_path, "main")
            assert score == pytest.approx(0.2, rel=1e-3)

    def test_lint_capped_at_03(self, tmp_path):
        changes = [{"status": "M", "path": "x.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch("bluei.engine.regression._find_test_deletions", return_value=[]),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch(
                "bluei.engine.regression._find_lint_regressions",
                return_value=[{"type": "lint_regression"} for _ in range(10)],
            ),
        ):
            score = compute_regression_score(tmp_path, "main")
            assert score <= 0.3 + 1e-9

    def test_combined_score(self, tmp_path):
        changes = [{"status": "D", "path": "tests/a.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch(
                "bluei.engine.regression._find_test_deletions",
                return_value=[{"type": "test_file_deleted"}],
            ),
            patch(
                "bluei.engine.regression._find_export_changes",
                return_value=[{"type": "module_init_deleted"}],
            ),
            patch(
                "bluei.engine.regression._find_lint_regressions",
                return_value=[{"type": "lint_regression"} for _ in range(2)],
            ),
        ):
            score = compute_regression_score(tmp_path, "main")
            expected = 0.15 + 0.2 + 0.2
            assert score == pytest.approx(expected, rel=1e-3)


class TestCheckRegressionsEdgeCases:
    """Additional check_regressions log-writing edge cases."""

    def test_writes_log_with_findings(self, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("")
        changes = [{"status": "D", "path": "tests/a.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch(
                "bluei.engine.regression._find_test_deletions",
                return_value=[
                    {
                        "type": "test_file_deleted",
                        "path": "tests/a.py",
                        "estimated_functions": 1,
                    }
                ],
            ),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            findings = check_regressions(tmp_path, "main", "feat", log)
            content = log.read_text()
            assert "1 finding(s)" in content
            assert "test_file_deleted" in content
            assert len(findings) == 1

    def test_writes_clean_message(self, tmp_path):
        log = tmp_path / "run.log"
        log.write_text("")
        changes = [{"status": "M", "path": "src/a.py"}]
        with (
            patch("bluei.engine.regression._git_diff_names", return_value=changes),
            patch("bluei.engine.regression._find_test_deletions", return_value=[]),
            patch("bluei.engine.regression._find_export_changes", return_value=[]),
            patch("bluei.engine.regression._find_lint_regressions", return_value=[]),
        ):
            findings = check_regressions(tmp_path, "main", "feat", log)
            assert findings == []
            content = log.read_text()
            assert "clean" in content
