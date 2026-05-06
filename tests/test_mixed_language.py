#!/usr/bin/env python3
"""Tests for mixed-language repository detection and handling.

Covers Zulip-style repos (Python backend + TypeScript/JS frontend) and similar
mixed-language patterns.
"""

import json
from pathlib import Path

import pytest

from qa_agent.models import LanguageInfo
from qa_agent.onboard import OnboardEngine, OnboardOptions


@pytest.fixture
def mixed_env(tmp_path):
    """Onboard engine with plugins and templates for language detection tests."""
    from qa_agent.config import ConfigManager
    from qa_agent.registry import RepoRegistry
    from qa_agent.health import HealthEngine
    from qa_agent.state import StateManager
    from qa_agent.plugins import PluginLoader

    workspace = tmp_path / 'qa-agent'
    (workspace / 'repos').mkdir(parents=True)
    (workspace / 'plugins').mkdir()

    repo_path = tmp_path / 'repo'
    repo_path.mkdir()

    config = ConfigManager(workspace)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)

    # Copy plugins
    source_plugins_dir = Path(__file__).resolve().parents[1] / 'plugins'
    if source_plugins_dir.exists():
        for plugin_dir in source_plugins_dir.iterdir():
            if plugin_dir.is_dir():
                target = workspace / 'plugins' / plugin_dir.name
                target.mkdir(parents=True, exist_ok=True)
                for child in plugin_dir.iterdir():
                    if child.is_file():
                        (target / child.name).write_text(child.read_text())

    # Copy templates so generate_config() works
    source_templates_dir = Path(__file__).resolve().parents[1] / 'templates' / 'repos'
    target_templates_dir = workspace / 'templates' / 'repos'
    target_templates_dir.mkdir(parents=True, exist_ok=True)
    if source_templates_dir.exists():
        for template_file in source_templates_dir.glob('*.yaml'):
            (target_templates_dir / template_file.name).write_text(template_file.read_text())

    plugins = PluginLoader(config.plugins_dir)
    engine = OnboardEngine(registry, plugins, health, state)

    return {
        'workspace': workspace,
        'repo_path': repo_path,
        'engine': engine,
    }


def make_repo(repo_path: Path, files: dict) -> None:
    """Create a repo with given files. Directories are created automatically."""
    for name, content in files.items():
        p = repo_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict):
            p.mkdir(parents=True, exist_ok=True)
            for subname, subcontent in content.items():
                subp = p / subname
                if subcontent is None:
                    subp.mkdir(parents=True, exist_ok=True)
                else:
                    subp.write_text(subcontent)
        else:
            p.write_text(content)


class TestDetectAllLanguages:
    """Test the detect_all_languages() method."""

    def test_single_python_repo(self, mixed_env):
        """Single-language Python repo returns only python."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
        })
        ranked = mixed_env['engine'].detect_all_languages(mixed_env['repo_path'])
        assert len(ranked) >= 1
        assert ranked[0][0] == 'python'

    def test_python_and_typescript_detected(self, mixed_env):
        """Repo with both Python and TypeScript markers returns both."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'react': '^18.0.0', 'typescript': '^5.0.0'},
                'scripts': {'test': 'vitest'}
            }),
        })
        ranked = mixed_env['engine'].detect_all_languages(mixed_env['repo_path'])
        langs = [lang for lang, score in ranked]
        assert 'python' in langs
        assert 'typescript' in langs

    def test_python_and_javascript_detected(self, mixed_env):
        """Repo with both Python and JavaScript returns both."""
        make_repo(mixed_env['repo_path'], {
            'pyproject.toml': '[project]\nname = "test"\n',
            'requirements.txt': 'django\n',
            'package.json': json.dumps({
                'dependencies': {'express': '^4.0.0'},
                'scripts': {'test': 'jest'}
            }),
        })
        ranked = mixed_env['engine'].detect_all_languages(mixed_env['repo_path'])
        langs = [lang for lang, score in ranked]
        assert 'python' in langs
        assert 'javascript' in langs

    def test_python_and_go_detected(self, mixed_env):
        """Repo with both Python and Go markers returns both."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'go.mod': 'module test\n\ngo 1.21',
        })
        ranked = mixed_env['engine'].detect_all_languages(mixed_env['repo_path'])
        langs = [lang for lang, score in ranked]
        assert 'python' in langs
        assert 'go' in langs

    def test_minimum_score_threshold(self, mixed_env):
        """Lock files alone don't trigger a false language detection."""
        make_repo(mixed_env['repo_path'], {
            'requirements.txt': 'flask\n',
            # No package.json — package-lock.json alone is not enough
        })
        ranked = mixed_env['engine'].detect_all_languages(mixed_env['repo_path'])
        langs = [lang for lang, score in ranked]
        assert 'javascript' not in langs
        assert 'typescript' not in langs

    def test_ranked_by_score_descending(self, mixed_env):
        """Languages are returned sorted by score descending."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\ndjango\npytest\n',
            'pyproject.toml': '[project]\nname = "test"\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'express': '^4.0.0'},
                'scripts': {}
            }),
        })
        ranked = mixed_env['engine'].detect_all_languages(mixed_env['repo_path'])
        scores = [score for lang, score in ranked]
        assert scores == sorted(scores, reverse=True)


class TestDetectLanguageSecondaryLanguages:
    """Test that detect_language() correctly records secondary languages."""

    def test_python_only_no_secondary(self, mixed_env):
        """Pure Python repo has no secondary languages."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python'
        assert lang.secondary_languages == []

    def test_python_plus_typescript_python_primary(self, mixed_env):
        """Python + TypeScript repo: Python primary when it has stronger signals.

        Python: setup.py(+1) + pyproject.toml(+1) + requirements.txt(+1) +
        setup.cfg(+1) + django in requirements(+2) = 6
        TypeScript: tsconfig(+1) + package.json(react deps +2) = 3
        → Python wins.
        """
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'pyproject.toml': '[project]\nname = "test"\n',
            'requirements.txt': 'flask\ndjango\npytest\n',
            'setup.cfg': '[metadata]\nname = test\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'react': '^18.0.0'},
                'scripts': {'test': 'vitest'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python', f"Expected python primary, got {lang.name}"
        assert 'typescript' in lang.secondary_languages

    def test_typescript_primary_python_secondary(self, mixed_env):
        """If TypeScript signals are much stronger, TypeScript is primary."""
        make_repo(mixed_env['repo_path'], {
            'requirements.txt': 'requests\n',  # weak Python signal (score=1)
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'typescript': '^5.0.0', '@types/node': '^20.0.0', 'react': '^18.0.0'},
                'scripts': {'test': 'vitest', 'lint': 'eslint'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'typescript', f"Expected typescript primary, got {lang.name}"
        assert 'python' in lang.secondary_languages

    def test_python_javascript_typescript_all_three(self, mixed_env):
        """Repo with all three: python primary when it has strongest signals."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'pyproject.toml': '[project]\nname = "test"\n',
            'requirements.txt': 'flask\ndjango\npytest\n',
            'setup.cfg': '[metadata]\nname = test\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'express': '^4.0.0'},
                'scripts': {'test': 'vitest'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python', f"Expected python primary, got {lang.name}"
        assert 'typescript' in lang.secondary_languages
        assert 'javascript' in lang.secondary_languages


class TestInferBaselineChecksMixed:
    """Test that baseline checks cover all languages in a mixed repo."""

    def test_python_plus_npm_infers_both(self, mixed_env):
        """Mixed Python + npm repo gets both pytest and npm test commands."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'tests/__init__.py': '',
            'package.json': json.dumps({
                'scripts': {'test': 'vitest', 'lint': 'eslint'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        checks = mixed_env['engine'].infer_baseline_checks(mixed_env['repo_path'], lang)
        cmd_strs = [' '.join(c) for c in checks]
        assert any('pytest' in c for c in cmd_strs), f"Expected pytest in checks: {cmd_strs}"
        assert any('vitest' in c or 'npm test' in c for c in cmd_strs), f"Expected vitest/npm test in checks: {cmd_strs}"

    def test_python_plus_npm_deduplicated(self, mixed_env):
        """Commands are deduplicated even when inferred from primary+secondary."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'tests/__init__.py': '',
            'package.json': json.dumps({
                'scripts': {'test': 'vitest'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        checks = mixed_env['engine'].infer_baseline_checks(mixed_env['repo_path'], lang)
        test_cmds = [c for c in checks if any(k in c[-1] for k in ('pytest', 'vitest', 'test'))]
        # Should have one Python test and one npm test — no duplicates
        assert len(test_cmds) >= 1

    def test_triple_language_all_checks(self, mixed_env):
        """Python + TypeScript + Go repo gets checks for all three."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'tests/__init__.py': '',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'scripts': {'test': 'vitest'}
            }),
            'go.mod': 'module test\n\ngo 1.21',
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        checks = mixed_env['engine'].infer_baseline_checks(mixed_env['repo_path'], lang)
        cmd_strs = [' '.join(c) for c in checks]
        assert any('pytest' in c for c in cmd_strs), f"Expected pytest in {cmd_strs}"
        assert any('vitest' in c or 'npm test' in c for c in cmd_strs), f"Expected npm/vitest in {cmd_strs}"
        assert any('go test' in c for c in cmd_strs), f"Expected go test in {cmd_strs}"


class TestSelectTemplateMixedLanguage:
    """Test that template selection handles mixed-language repos sensibly."""

    def test_python_primary_selects_python_template(self, mixed_env):
        """When Python is primary, select Python template."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'pyproject.toml': '[project]\nname = "test"\n',
            'requirements.txt': 'flask\ndjango\npytest\n',
            'setup.cfg': '[metadata]\nname = test\n',
            'manage.py': '# manage',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'react': '^18.0.0'},
                'scripts': {}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python', f"Expected python primary, got {lang.name}"
        tmpl = mixed_env['engine'].select_template(mixed_env['repo_path'], lang, None)
        assert tmpl == 'django-app'

    def test_typescript_primary_selects_ts_template(self, mixed_env):
        """When TypeScript is primary, select TypeScript template."""
        make_repo(mixed_env['repo_path'], {
            'requirements.txt': 'requests\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'typescript': '^5.0.0', '@types/node': '^20.0.0', 'react': '^18.0.0'},
                'scripts': {'test': 'vitest', 'build': 'vite build'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'typescript', f"Expected typescript primary, got {lang.name}"
        tmpl = mixed_env['engine'].select_template(mixed_env['repo_path'], lang, None)
        assert tmpl in ('react-app', 'next-app', 'node-library')


class TestGenerateConfigMixedLanguage:
    """Test that generate_config stores secondary languages in meta."""

    def test_secondary_languages_in_meta(self, mixed_env):
        """RepoConfig meta should include secondary_languages."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'pyproject.toml': '[project]\nname = "test"\n',
            'setup.cfg': '[metadata]\nname = test\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'dependencies': {'react': '^18.0.0'},
                'scripts': {}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python'
        assert 'typescript' in lang.secondary_languages
        plugin_id = mixed_env['engine'].select_plugin(lang.name, None)
        config = mixed_env['engine'].generate_config(
            mixed_env['repo_path'], 'mixed-repo', lang, None, plugin_id
        )
        assert 'secondary_languages' in config.meta
        assert 'typescript' in config.meta['secondary_languages']


class TestBuildReviewItemsMixedLanguage:
    """Test that build_review_items produces appropriate items for mixed repos."""

    def test_mixed_language_review_item(self, mixed_env):
        """Mixed-language repos should generate a review item warning."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
            'pyproject.toml': '[project]\nname = "test"\n',
            'setup.cfg': '[metadata]\nname = test\n',
            'tsconfig.json': '{}',
            'package.json': json.dumps({'dependencies': {}, 'scripts': {}}),
        })
        from qa_agent.models import generate_id
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python'
        plugin_id = mixed_env['engine'].select_plugin(lang.name, None)
        config = mixed_env['engine'].generate_config(
            mixed_env['repo_path'], 'test-mixed', lang, None, plugin_id
        )
        items = mixed_env['engine'].build_review_items(mixed_env['repo_path'], lang, config)
        mixed_item = next((i for i in items if 'mixed-language' in i.lower() or 'secondary' in i.lower()), None)
        assert mixed_item is not None, f"Expected mixed-language review item in {items}"
        assert 'python' in mixed_item
        assert 'typescript' in mixed_item

    def test_single_language_no_mixed_review_item(self, mixed_env):
        """Single-language repos should NOT get a mixed-language review item."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\n',
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        plugin_id = mixed_env['engine'].select_plugin(lang.name, None)
        config = mixed_env['engine'].generate_config(
            mixed_env['repo_path'], 'test-single', lang, None, plugin_id
        )
        items = mixed_env['engine'].build_review_items(mixed_env['repo_path'], lang, config)
        mixed_item = next((i for i in items if 'mixed-language' in i), None)
        assert mixed_item is None, f"Unexpected mixed-language review item for single-language repo: {items}"


class TestZulipStyleRepo:
    """Test a realistic Zulip-style repo: Python backend + TypeScript frontend."""

    def test_zulip_style_detection(self, mixed_env):
        """Zulip-style: Python backend (strong), TypeScript frontend → Python primary.

        To ensure Python wins, we add multiple Python markers:
        - setup.py, pyproject.toml, requirements.txt, setup.cfg, Pipfile (+5 markers)
        - Django framework boost from requirements.txt (+2)
        Total Python: ~7

        TypeScript would get: tsconfig(+1) + @types/+typescript deps(+7) = 8
        So we add more Python markers to win.
        """
        make_repo(mixed_env['repo_path'], {
            # Python backend signals (very strong)
            'setup.py': '# setup',
            'pyproject.toml': '[project]\nname = "zulip"\nversion = "1.0.0"\n',
            'requirements.txt': 'flask\ndjango\npsycopg2\ncelery\n',
            'setup.cfg': '[metadata]\nname = zulip\n',
            'Pipfile': '[[source]]\nurl = "https://pypi.org/simple"\n',
            'manage.py': '# manage',
            # TypeScript frontend signals (moderate)
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'name': 'zulip-web',
                'dependencies': {'react': '^18.0.0'},
                'scripts': {'test': 'jest', 'build': 'tsc', 'lint': 'eslint'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        assert lang.name == 'python', f"Expected python primary (Zulip is Python-backend), got {lang.name}"
        assert 'typescript' in lang.secondary_languages

    def test_zulip_style_baseline_checks(self, mixed_env):
        """Zulip-style repo gets Python pytest AND npm test commands."""
        make_repo(mixed_env['repo_path'], {
            'setup.py': '# setup',
            'requirements.txt': 'flask\ndjango\n',
            'tests/__init__.py': '',
            'tsconfig.json': '{}',
            'package.json': json.dumps({
                'scripts': {'test': 'jest', 'lint': 'eslint'}
            }),
        })
        lang = mixed_env['engine'].detect_language(mixed_env['repo_path'])
        checks = mixed_env['engine'].infer_baseline_checks(mixed_env['repo_path'], lang)
        cmd_strs = [' '.join(c) for c in checks]
        assert any('pytest' in c for c in cmd_strs), f"Expected pytest in {cmd_strs}"
        assert any('jest' in c or 'npm test' in c for c in cmd_strs), f"Expected jest/npm test in {cmd_strs}"
