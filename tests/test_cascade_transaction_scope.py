from unittest.mock import patch

import pytest

from bluei.engine.cascade import CascadeContext, CascadeResult, CascadeStage, DeterministicCascade, default_linter_stages
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
