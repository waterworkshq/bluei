"""Tests for the regression detection module."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.sandbox_local_runner.regression import (
    _git_diff_names,
    _find_test_deletions,
    _find_export_changes,
    check_regressions,
)


class TestGitDiffNames:
    """git diff --name-status parsing."""

    def test_empty_output(self, tmp_path: Path) -> None:
        with patch('core.sandbox_local_runner.regression.run_capture', return_value=(1, '')):
            result = _git_diff_names(tmp_path, 'main', 'feature')
            assert result == []

    def test_parses_changes(self, tmp_path: Path) -> None:
        output = (
            "M\tsrc/file1.py\n"
            "A\tsrc/file2.py\n"
            "D\ttests/test_file.py\n"
        )
        with patch('core.sandbox_local_runner.regression.run_capture', return_value=(0, output)):
            result = _git_diff_names(tmp_path, 'main', 'feature')
            assert len(result) == 3
            assert result[0] == {'status': 'M', 'path': 'src/file1.py'}
            assert result[1] == {'status': 'A', 'path': 'src/file2.py'}
            assert result[2] == {'status': 'D', 'path': 'tests/test_file.py'}


class TestFindTestDeletions:
    """Test file deletion detection."""

    def test_flags_deleted_test_file(self, tmp_path: Path) -> None:
        changes = [{'status': 'D', 'path': 'tests/test_foo.py'}]
        with patch('core.sandbox_local_runner.regression.run_capture',
                   return_value=(0, 'def test_bar(): ...\ndef test_baz(): ...\n')):
            result = _find_test_deletions(changes, tmp_path, 'main')
            assert len(result) == 1
            assert result[0]['type'] == 'test_file_deleted'
            assert result[0]['path'] == 'tests/test_foo.py'
            assert result[0]['estimated_functions'] == 2

    def test_ignores_non_test_deletions(self, tmp_path: Path) -> None:
        changes = [{'status': 'D', 'path': 'src/foo.py'}]
        result = _find_test_deletions(changes, tmp_path, 'main')
        assert result == []

    def test_ignores_additions_and_modifications(self, tmp_path: Path) -> None:
        changes = [
            {'status': 'A', 'path': 'tests/test_new.py'},
            {'status': 'M', 'path': 'tests/test_existing.py'},
        ]
        result = _find_test_deletions(changes, tmp_path, 'main')
        assert result == []

    def test_detects_test_prefixes(self, tmp_path: Path) -> None:
        changes = [
            {'status': 'D', 'path': 'test_foo.py'},
            {'status': 'D', 'path': 'spec/bar_spec.rb'},
            {'status': 'D', 'path': '__tests__/baz.test.js'},
        ]
        with patch('core.sandbox_local_runner.regression.run_capture', return_value=(0, '')):
            result = _find_test_deletions(changes, tmp_path, 'main')
            assert len(result) == 3


class TestFindExportChanges:
    """Export surface change detection."""

    def test_flags_init_py_deletion(self) -> None:
        changes = [{'status': 'D', 'path': 'mypackage/__init__.py'}]
        result = _find_export_changes(changes, Path('/tmp'))
        assert len(result) == 1
        assert result[0]['type'] == 'module_init_deleted'

    def test_flags_index_ts_deletion(self) -> None:
        changes = [{'status': 'D', 'path': 'src/index.ts'}]
        result = _find_export_changes(changes, Path('/tmp'))
        assert len(result) == 1
        assert result[0]['type'] == 'export_module_deleted'

    def test_ignores_source_removal(self) -> None:
        changes = [{'status': 'D', 'path': 'src/util.py'}]
        result = _find_export_changes(changes, Path('/tmp'))
        assert result == []


class TestCheckRegressions:
    """Full regression check integration."""

    def test_clean_diff_no_findings(self, tmp_path: Path) -> None:
        log_file = tmp_path / 'run.log'
        log_file.write_text('')
        with patch('core.sandbox_local_runner.regression._git_diff_names', return_value=[]):
            result = check_regressions(tmp_path, 'main', 'feature', log_file)
            assert result == []

    def test_finds_test_deletion(self, tmp_path: Path) -> None:
        log_file = tmp_path / 'run.log'
        log_file.write_text('')
        mock_changes = [{'status': 'D', 'path': 'tests/test_foo.py'}]
        with patch('core.sandbox_local_runner.regression._git_diff_names', return_value=mock_changes), \
             patch('core.sandbox_local_runner.regression._find_test_deletions',
                   return_value=[{'type': 'test_file_deleted', 'path': 'tests/test_foo.py',
                                  'estimated_functions': 2}]), \
             patch('core.sandbox_local_runner.regression._find_export_changes', return_value=[]), \
             patch('core.sandbox_local_runner.regression._find_lint_regressions', return_value=[]):
            result = check_regressions(tmp_path, 'main', 'feature', log_file)
            assert len(result) == 1
            assert result[0]['type'] == 'test_file_deleted'
