from unittest.mock import patch

import pytest

from bluei.engine import lifecycle


def test_autofix_recipe_failure_restores_before_return(tmp_path, make_finding):
    target = tmp_path / 'target.py'
    target.write_text('original\n')
    finding = make_finding(path='target.py', rule='unmatched-rule')

    class Engine:
        def match(self, *args, **kwargs):
            return type('Recipe', (), {'id': 'test'})()

        def apply(self, recipe, file_path, worktree, finding=None):
            file_path.write_text('partial edit\n')
            return type('Result', (), {'success': False, 'error': 'command failed'})()

    with patch.object(lifecycle, '_get_recipe_engine', return_value=Engine()):
        assert not lifecycle.apply_autofix(tmp_path, finding, tmp_path / 'log')
    assert target.read_text() == 'original\n'


def test_autofix_exception_restores(tmp_path, make_finding):
    target = tmp_path / 'target.py'
    target.write_text('original\n')
    finding = make_finding(path='target.py')

    class Engine:
        def match(self, *args, **kwargs):
            return type('Recipe', (), {'id': 'test'})()

        def apply(self, recipe, file_path, worktree, finding=None):
            file_path.write_text('partial edit\n')
            raise RuntimeError('broken')

    with patch.object(lifecycle, '_get_recipe_engine', return_value=Engine()):
        with pytest.raises(RuntimeError, match='broken'):
            lifecycle.apply_autofix(tmp_path, finding, tmp_path / 'log')
    assert target.read_text() == 'original\n'


@pytest.mark.parametrize('error', [OSError('cleanup failed'), KeyboardInterrupt()])
def test_accepted_autofix_cleanup_never_restores_deleted_backup(tmp_path, make_finding, error):
    from bluei.engine import worktree_checkpoint as checkpoints

    target = tmp_path / 'target.py'
    target.write_text('original\n')
    real_discard = checkpoints.discard_checkpoint
    restore_calls = []

    @lifecycle._transactional_autofix
    def fix(worktree, finding, log, pattern_store=None, detected_frameworks=None):
        target.write_text('accepted\n')
        return True

    def interrupted_discard(cp):
        real_discard(cp)
        raise error

    with patch.object(checkpoints, 'discard_checkpoint', side_effect=interrupted_discard), \
            patch.object(checkpoints, 'restore_checkpoint', side_effect=lambda cp: restore_calls.append(cp)), \
            patch.object(lifecycle, '_extract_fix_pattern'):
        if isinstance(error, OSError):
            assert fix(tmp_path, make_finding(path='target.py'), tmp_path / 'log')
        else:
            with pytest.raises(KeyboardInterrupt):
                fix(tmp_path, make_finding(path='target.py'), tmp_path / 'log')
    assert target.read_text() == 'accepted\n'
    assert restore_calls == []
