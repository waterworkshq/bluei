#!/usr/bin/env python3
"""Tests for TransientArtifactHealer - self-heal mechanism for transient artifact dirty trees."""

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure qa_agent is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa_agent.healer import (
    TransientArtifactHealer,
    TRANSIENT_ARTIFACTS,
    _transient_matcher,
    _git_is_dirty,
    _git_status_porcelain,
)


def _git(*args, cwd=None):
    """Run a git command; raises if git is not available."""
    result = subprocess.run(
        ['bash', '-lc', ' '.join(['git'] + list(args))],
        cwd=str(cwd),
        text=True,
        capture_output=True,
    )
    return result


@pytest.fixture
def git_repo(tmp_path):
    """A bare git repo for testing."""
    repo = tmp_path / 'repo.git'
    _git('init', '--bare', cwd=tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    _git('init', cwd=work)
    _git('config', 'user.email', 'test@test.com', cwd=work)
    _git('config', 'user.name', 'Test', cwd=work)
    # Commit something so the repo is "real"
    (work / 'README').write_text('hello')
    _git('add', 'README', cwd=work)
    _git('commit', '-m', 'initial', cwd=work)
    yield work


class TestTransientMatcher:
    """Unit tests for the _transient_matcher helper."""

    @pytest.mark.parametrize('name,expected', [
        ('__pycache__', True),
        ('*.pyc', True),
        ('node_modules', True),
        ('coverage', True),
        ('.nyc_output', True),
        ('.pytest_cache', True),
        ('src', False),  # not a transient pattern
        ('main.py', False),
        ('package.json', False),
    ])
    def test_matcher(self, name, expected):
        # Check if any TRANSIENT_ARTIFACT pattern matches
        is_transient = _transient_matcher(name, True) or _transient_matcher(name, False)
        assert is_transient == expected, f'{name} should be transient={expected}'


class TestTransientArtifactHealerGitStatus:
    """Test healer detection of dirty trees."""

    def test_clean_tree_not_dirty(self, git_repo):
        healer = TransientArtifactHealer(git_repo)
        assert not healer.is_dirty()
        assert healer.get_dirty_paths() == []

    def test_untracked_file_detected(self, git_repo):
        (git_repo / 'newfile.txt').write_text('hello')
        healer = TransientArtifactHealer(git_repo)
        assert healer.is_dirty()
        assert 'newfile.txt' in healer.get_dirty_paths()

    def test_modified_tracked_file_detected(self, git_repo):
        (git_repo / 'README').write_text('modified')
        _git('add', 'README', cwd=git_repo)
        healer = TransientArtifactHealer(git_repo)
        assert healer.is_dirty()

    def test_transient_dirty_exclusively(self, git_repo):
        """Only transient artifacts are dirty → is_exclusively_transient_dirty returns True."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.cpython-313.pyc').write_text('bytecode')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        healer = TransientArtifactHealer(git_repo)
        assert healer.is_dirty()
        assert healer.is_exclusively_transient_dirty()

    def test_mixed_dirty_not_exclusively_transient(self, git_repo):
        """Mixed transient + non-transient → is_exclusively_transient_dirty returns False."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.cpython-313.pyc').write_text('bytecode')
        (git_repo / 'important.txt').write_text('important work in progress')
        healer = TransientArtifactHealer(git_repo)
        assert healer.is_dirty()
        assert not healer.is_exclusively_transient_dirty()

    def test_non_transient_only_not_exclusively(self, git_repo):
        """Only non-transient files dirty → is_exclusively_transient_dirty returns False."""
        (git_repo / 'important.txt').write_text('important work in progress')
        healer = TransientArtifactHealer(git_repo)
        assert healer.is_dirty()
        assert not healer.is_exclusively_transient_dirty()


class TestHealerGitignoreHealing:
    """Test .gitignore healing."""

    def test_heal_gitignore_dry_run_does_not_write(self, git_repo):
        """Dry-run should not modify .gitignore."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        healer = TransientArtifactHealer(git_repo)
        changed, entries = healer.heal_gitignore(dry_run=True)
        assert changed  # entries would be added
        assert not (git_repo / '.gitignore').exists() or 'QA Agent' not in (git_repo / '.gitignore').read_text()

    def test_heal_gitignore_writes_entries(self, git_repo):
        """Actual heal should add entries to .gitignore."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        healer = TransientArtifactHealer(git_repo)
        changed, entries = healer.heal_gitignore(dry_run=False)
        assert changed
        assert '__pycache__' in entries or 'coverage' in entries
        gitignore = (git_repo / '.gitignore').read_text()
        assert '__pycache__' in gitignore or 'coverage' in gitignore

    def test_get_missing_gitignore_entries(self, git_repo):
        """Should return transient patterns not already in .gitignore."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        healer = TransientArtifactHealer(git_repo)
        missing = healer.get_missing_gitignore_entries()
        # __pycache__/coverage should be missing from empty .gitignore
        assert '__pycache__' in missing or 'coverage' in missing

    def test_already_ignored_not_missing(self, git_repo):
        """Transient patterns already in .gitignore should not be in missing."""
        (git_repo / '.gitignore').write_text('__pycache__\ncoverage\n')
        (git_repo / '__pycache__').mkdir()
        (git_repo / 'coverage').mkdir()
        healer = TransientArtifactHealer(git_repo)
        missing = healer.get_missing_gitignore_entries()
        assert '__pycache__' not in missing
        assert 'coverage' not in missing


class TestHealerArtifactRemoval:
    """Test artifact removal healing."""

    def test_heal_remove_artifacts_dry_run(self, git_repo):
        """Dry-run should not actually remove artifacts."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.cpython-313.pyc').write_text('bytecode')
        healer = TransientArtifactHealer(git_repo)
        changed, removed = healer.heal_remove_artifacts(dry_run=True)
        assert changed
        assert '__pycache__' in removed[0] or '__pycache__' in str(removed)
        assert (git_repo / '__pycache__').exists()  # not actually removed

    def test_heal_remove_artifacts_actually_removes(self, git_repo):
        """Actual call should remove transient artifact dirs/files."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.cpython-313.pyc').write_text('bytecode')
        healer = TransientArtifactHealer(git_repo)
        changed, removed = healer.heal_remove_artifacts(dry_run=False)
        assert changed
        assert not (git_repo / '__pycache__').exists()

    def test_heal_full_dry_run_reports_intent(self, git_repo):
        """Full heal dry-run should report what would happen."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        healer = TransientArtifactHealer(git_repo)
        result = healer.heal(remove_artifacts=True, dry_run=True)
        assert result['dry_run'] is True
        assert result['is_exclusively_transient'] is True
        assert len(result['gitignore_entries_added']) > 0 or len(result['artifacts_removed']) > 0

    def test_heal_full_actually_heals(self, git_repo):
        """Full heal should update .gitignore and remove artifacts."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('byte')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        healer = TransientArtifactHealer(git_repo)
        result = healer.heal(remove_artifacts=True, dry_run=False)
        assert not (git_repo / '__pycache__').exists()
        assert not (git_repo / 'coverage').exists()
        gitignore = (git_repo / '.gitignore').read_text()
        assert 'QA Agent transient' in gitignore


class TestSafeToAutoheal:
    """Test safe_to_autoheal logic."""

    def test_clean_tree_safe(self, git_repo):
        """Clean tree is always safe to report as auto-healable."""
        healer = TransientArtifactHealer(git_repo)
        safe, reason = healer.safe_to_autoheal(require_clean_worktree=True)
        assert safe
        assert 'already clean' in reason

    def test_require_clean_false_unsafe(self, git_repo):
        """If require_clean_worktree is False, autoheal is not applied."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        healer = TransientArtifactHealer(git_repo)
        safe, reason = healer.safe_to_autoheal(require_clean_worktree=False)
        assert not safe

    def test_transient_only_safe(self, git_repo):
        """Exclusively transient dirty is safe."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        (git_repo / '.nyc_output').mkdir()
        (git_repo / '.nyc_output' / 'out.json').write_text('{}')
        healer = TransientArtifactHealer(git_repo)
        safe, reason = healer.safe_to_autoheal(require_clean_worktree=True)
        assert safe
        assert 'safe to auto-heal' in reason

    def test_mixed_unsafe(self, git_repo):
        """Mixed dirty paths are not safe to auto-heal."""
        (git_repo / '__pycache__').mkdir()
        (git_repo / 'important.txt').write_text('important')
        healer = TransientArtifactHealer(git_repo)
        safe, reason = healer.safe_to_autoheal(require_clean_worktree=True)
        assert not safe
        assert 'non-transient' in reason


class TestPreFlightAutohealReporting:
    """Test that preflight autoheal note is correctly set."""

    def test_preflight_checks_healer_on_dirty_tree(self, git_repo):
        """The healer should correctly identify transient-artifact-only dirty."""
        # Create only transient artifacts (with files inside so git tracks them)
        (git_repo / '__pycache__').mkdir()
        (git_repo / '__pycache__' / 'test.pyc').write_text('bytecode')
        (git_repo / 'coverage').mkdir()
        (git_repo / 'coverage' / 'report.txt').write_text('cov')
        (git_repo / '.nyc_output').mkdir()
        (git_repo / '.nyc_output' / 'out.json').write_text('{}')

        healer = TransientArtifactHealer(git_repo)
        assert healer.is_dirty()
        assert healer.is_exclusively_transient_dirty()
        safe, _ = healer.safe_to_autoheal(require_clean_worktree=True)
        assert safe


# --- fnmatch dependency test ---
class TestPatternCatalog:
    """Sanity-check the TRANSIENT_ARTIFACTS catalog."""

    def test_no_empty_patterns(self):
        for pattern, is_dir in TRANSIENT_ARTIFACTS:
            assert pattern, "Empty pattern in TRANSIENT_ARTIFACTS"
            assert isinstance(is_dir, bool), f"is_dir should be bool for {pattern}"

    def test_coverage_and_nyc_covered(self):
        names = [p for p, _ in TRANSIENT_ARTIFACTS]
        assert 'coverage' in names
        assert '.nyc_output' in names
        assert '__pycache__' in names
        assert 'node_modules' in names
        assert '.pytest_cache' in names
        assert '.ruff_cache' in names
        assert '.mypy_cache' in names
