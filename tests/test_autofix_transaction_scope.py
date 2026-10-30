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
        expected = checkpoints.CheckpointError if isinstance(error, OSError) else KeyboardInterrupt
        with pytest.raises(expected):
            fix(tmp_path, make_finding(path='target.py'), tmp_path / 'log')
    assert target.read_text() == 'accepted\n'
    assert restore_calls == []


def test_cleanup_interrupt_before_discard_closes_git_descriptor(tmp_path, make_finding):
    import os
    from bluei.engine import worktree_checkpoint as checkpoints

    (tmp_path / '.git').mkdir()
    (tmp_path / '.git/index').write_bytes(b'index')
    target = tmp_path / 'target.py'
    target.write_text('original\n')
    observed = []

    @lifecycle._transactional_autofix
    def fix(worktree, finding, log, pattern_store=None, detected_frameworks=None):
        target.write_text('accepted\n')
        return True

    def interrupt(cp):
        observed.append((cp, cp.git_dir_fd))
        raise KeyboardInterrupt

    with patch.object(checkpoints, 'discard_checkpoint', side_effect=interrupt), \
            patch.object(checkpoints, 'restore_checkpoint') as restore, \
            patch.object(lifecycle, '_extract_fix_pattern'):
        with pytest.raises(KeyboardInterrupt):
            fix(tmp_path, make_finding(path='target.py'), tmp_path / 'log')
    restore.assert_not_called()
    cp, descriptor = observed[0]
    assert cp.git_dir_fd is None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert target.read_text() == 'accepted\n'
    checkpoints.discard_checkpoint(cp)
