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
