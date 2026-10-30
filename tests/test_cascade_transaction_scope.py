from unittest.mock import patch

import subprocess

import pytest

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStage,
    CompositePatternCascadeStage,
    DeterministicCascade,
    default_linter_stages,
)
from bluei.engine.composite_pattern import (
    CompositePattern,
    CompositePatternStep,
    CompositePatternStore,
)
from bluei.engine.lifecycle import apply_cascade_fix


class Rogue(CascadeStage):
    name = 'rogue'
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        (worktree / finding.path).write_text('fixed')
        (worktree / 'other.py').write_text('unauthorized')
        return CascadeResult(True, self.name, [finding.path, 'other.py'], True)


def test_claimed_path_does_not_authorize_change(tmp_path, make_finding):
    (tmp_path / 'target.py').write_text('original')
    (tmp_path / 'other.py').write_text('original other')
    finding = make_finding(path='target.py')
    result = DeterministicCascade([Rogue()]).execute(finding, tmp_path, CascadeContext(log_file=tmp_path / 'log'))
    assert not result.success
    assert (tmp_path / 'target.py').read_text() == 'original'
    assert (tmp_path / 'other.py').read_text() == 'original other'


@pytest.mark.parametrize('rule,safe', [('ruff-F40', True), ('ruff-F401', False), ('ruff-F401,ALL', True)])
def test_ruff_refuses_malformed_or_non_autofixable(tmp_path, make_finding, rule, safe):
    stage = default_linter_stages()[0]
    finding = make_finding(path='target.py', rule=rule, safe_to_autofix=safe)
    (tmp_path / 'target.py').write_text('import os\n')
    with patch('bluei.engine.cascade.run_capture') as run:
        assert not stage.attempt(finding, tmp_path, CascadeContext(log_file=tmp_path / 'log')).success
        run.assert_not_called()


@pytest.mark.parametrize('failure', [False, RuntimeError('tier broke')])
def test_outer_tier_rejection_restores_and_never_reports_success(tmp_path, make_finding, failure):
    class Fix(Rogue):
        def attempt(self, finding, worktree, context):
            (worktree / finding.path).write_text('fixed')
            return CascadeResult(True, self.name, [finding.path], True)

    target = tmp_path / 'target.py'
    target.write_text('original')
    records = []
    with patch('bluei.engine.cascade.default_cascade_stages', return_value=[Fix()]), patch('bluei.engine.lifecycle._tier_validate', side_effect=failure if isinstance(failure, Exception) else None, return_value=False), patch('bluei.engine.lifecycle._extract_fix_pattern'):
        if isinstance(failure, Exception):
            with pytest.raises(RuntimeError, match='tier broke'):
                apply_cascade_fix(tmp_path, make_finding(path='target.py'), tmp_path / 'log', deterministic_only=True, ledger_records=records)
        else:
            assert not apply_cascade_fix(tmp_path, make_finding(path='target.py'), tmp_path / 'log', deterministic_only=True, ledger_records=records)
    assert target.read_text() == 'original'
    assert all(row['outcome'] != 'resolved_deterministic' for row in records)


# ── Post-tier scope recheck: _tier_validate runs AFTER execute() verified the
# declared stage scope, so it must not be able to smuggle undeclared file or
# Git-index edits past learning / success recording / checkpoint discard. ──


def _git(worktree, *args):
    subprocess.run(['git', *args], cwd=str(worktree), check=True, capture_output=True)


def _real_repo(tmp_path):
    wt = tmp_path / 'wt'
    wt.mkdir()
    _git(wt, 'init')
    _git(wt, 'config', 'user.email', 't@t.example')
    _git(wt, 'config', 'user.name', 'T')
    (wt / 'target.py').write_text('original\n')
    (wt / 'other.py').write_text('other\n')
    _git(wt, 'add', '-A')
    _git(wt, 'commit', '-m', 'init')
    return wt


class Fixed(CascadeStage):
    name = 'fixed'
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        (worktree / finding.path).write_text('fixed\n')
        return CascadeResult(True, self.name, [finding.path], True)


def test_tier_validate_undeclared_write_rejected_and_restored(tmp_path, make_finding):
    wt = _real_repo(tmp_path)
    records = []

    def rogue_tier(tier, worktree_path, finding, log_file, state=None):
        (worktree_path / 'other.py').write_text('hijacked\n')
        return True

    with patch('bluei.engine.cascade.default_cascade_stages', return_value=[Fixed()]), \
            patch('bluei.engine.lifecycle._tier_validate', side_effect=rogue_tier), \
            patch('bluei.engine.lifecycle._extract_fix_pattern') as learn:
        accepted = apply_cascade_fix(wt, make_finding(path='target.py'), tmp_path / 'log',
                                     deterministic_only=True, ledger_records=records)
    assert not accepted
    assert (wt / 'target.py').read_text() == 'original\n'
    assert (wt / 'other.py').read_text() == 'other\n'
    assert learn.call_count == 0
    assert [row['outcome'] for row in records] == ['exhausted']


def test_tier_validate_index_mutation_rejected_and_restored(tmp_path, make_finding):
    wt = _real_repo(tmp_path)
    records = []

    def rogue_tier(tier, worktree_path, finding, log_file, state=None):
        # Index-only edit: file bytes on disk are untouched.
        _git(worktree_path, 'rm', '--cached', '-q', 'other.py')
        return True

    with patch('bluei.engine.cascade.default_cascade_stages', return_value=[Fixed()]), \
            patch('bluei.engine.lifecycle._tier_validate', side_effect=rogue_tier), \
            patch('bluei.engine.lifecycle._extract_fix_pattern') as learn:
        accepted = apply_cascade_fix(wt, make_finding(path='target.py'), tmp_path / 'log',
                                     deterministic_only=True, ledger_records=records)
    assert not accepted
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=str(wt),
                            check=True, capture_output=True, text=True).stdout
    assert status.strip() == ''  # index restored: other.py still tracked
    assert (wt / 'other.py').read_text() == 'other\n'
    assert learn.call_count == 0
    assert [row['outcome'] for row in records] == ['exhausted']


def test_normal_fix_with_real_tier_validation_still_accepted(tmp_path, make_finding):
    wt = _real_repo(tmp_path)
    records = []
    with patch('bluei.engine.cascade.default_cascade_stages', return_value=[Fixed()]), \
            patch('bluei.engine.lifecycle._extract_fix_pattern') as learn:
        accepted = apply_cascade_fix(wt, make_finding(path='target.py'), tmp_path / 'log',
                                     deterministic_only=True, ledger_records=records)
    assert accepted
    assert (wt / 'target.py').read_text() == 'fixed\n'
    assert learn.call_count == 1
    assert [row['outcome'] for row in records] == ['resolved_deterministic']


def test_composite_declared_scope_still_accepted(tmp_path, make_finding):
    wt = _real_repo(tmp_path)
    (wt / 'pkg').mkdir()
    (wt / 'pkg' / 'helper.py').write_text('VALUE = 1\n')
    _git(wt, 'add', '-A')
    _git(wt, 'commit', '-m', 'pkg')

    store_path = tmp_path / 'state' / 'patterns' / 'fix_patterns.jsonl'
    comp_store = CompositePatternStore(store_path.with_name('composite_patterns.jsonl'))
    comp_store.append(CompositePattern(
        pattern_id='cp1', rule='comp-rule', language='python', confidence=0.9,
        steps=[
            CompositePatternStep(order=1, description='finding', match='original',
                                 replacement='fixed', scope='finding_location'),
            CompositePatternStep(order=2, description='related', match='VALUE = 1',
                                 replacement='VALUE = 2', scope='related_file',
                                 file_pattern='pkg/helper.py'),
        ],
    ))
    records = []
    with patch('bluei.engine.cascade.default_cascade_stages',
               return_value=[CompositePatternCascadeStage()]), \
            patch('bluei.engine.validation.verify_fix_closed',
                  return_value=type('R', (), {'is_closed': True})()), \
            patch('bluei.engine.lifecycle._extract_fix_pattern') as learn:
        accepted = apply_cascade_fix(wt, make_finding(path='target.py', rule='comp-rule'),
                                     tmp_path / 'log', pattern_store_path=store_path,
                                     deterministic_only=True, ledger_records=records)
    assert accepted
    assert (wt / 'target.py').read_text() == 'fixed\n'
    assert (wt / 'pkg' / 'helper.py').read_text() == 'VALUE = 2\n'
    assert learn.call_count == 1
    assert [row['outcome'] for row in records] == ['resolved_deterministic']
