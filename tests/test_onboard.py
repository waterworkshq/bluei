#!/usr/bin/env python3
"""Pytest-native tests for Onboarding Engine."""

from pathlib import Path
import json

import pytest

from qa_agent.models import RepoStatus
from qa_agent.config import ConfigManager
from qa_agent.registry import RepoRegistry
from qa_agent.health import HealthEngine
from qa_agent.state import StateManager
from qa_agent.plugins import PluginLoader
from qa_agent.onboard import OnboardEngine, OnboardOptions


@pytest.fixture
def onboard_env(tmp_path):
    workspace = tmp_path / 'qa-agent'
    (workspace / 'repos').mkdir(parents=True)
    (workspace / 'plugins').mkdir()

    repo_path = tmp_path / 'repo-under-test'
    repo_path.mkdir()

    config = ConfigManager(workspace)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    source_plugins_dir = Path(__file__).resolve().parents[1] / 'plugins'
    for plugin_dir in source_plugins_dir.iterdir():
        if plugin_dir.is_dir():
            target = workspace / 'plugins' / plugin_dir.name
            target.mkdir(parents=True, exist_ok=True)
            for child in plugin_dir.iterdir():
                target_child = target / child.name
                if child.is_file():
                    target_child.write_text(child.read_text())
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
        'config': config,
        'registry': registry,
        'health': health,
        'state': state,
        'plugins': plugins,
        'engine': engine,
    }


def test_detect_python_language(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'setup.py').write_text('# setup')
    (repo / 'requirements.txt').write_text('flask')
    lang = onboard_env['engine'].detect_language(repo)
    assert lang.name == 'python'


def test_detect_typescript_language(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'tsconfig.json').write_text('{}')
    (repo / 'package.json').write_text(json.dumps({'dependencies': {'typescript': '^4.0.0'}}))
    lang = onboard_env['engine'].detect_language(repo)
    assert lang.name == 'typescript'


def test_detect_javascript_language(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'package.json').write_text(json.dumps({'dependencies': {'express': '^4.0.0'}}))
    lang = onboard_env['engine'].detect_language(repo)
    assert lang.name == 'javascript'


def test_detect_go_language(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'go.mod').write_text('module test\n\ngo 1.21')
    lang = onboard_env['engine'].detect_language(repo)
    assert lang.name == 'go'


def test_select_template_for_go_repo(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'go.mod').write_text('module test\n\ngo 1.21')
    lang = onboard_env['engine'].detect_language(repo)
    template = onboard_env['engine'].select_template(repo, lang, None)
    assert template == 'go-service'


def test_detect_rust_language(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'Cargo.toml').write_text('[package]\nname = "test"')
    lang = onboard_env['engine'].detect_language(repo)
    assert lang.name == 'rust'


def test_select_template_for_rust_repo(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'Cargo.toml').write_text('[package]\nname = "test"')
    lang = onboard_env['engine'].detect_language(repo)
    template = onboard_env['engine'].select_template(repo, lang, None)
    assert template == 'rust-crate'


def test_detect_unknown_language(onboard_env):
    lang = onboard_env['engine'].detect_language(onboard_env['repo_path'])
    assert lang.name == 'unknown'


def test_detect_python_framework_django(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'manage.py').write_text('# manage')
    (repo / 'requirements.txt').write_text('django')
    framework = onboard_env['engine'].detect_framework(repo, 'python')
    assert framework == 'django'


def test_select_template_for_python_django_repo(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'manage.py').write_text('# manage')
    (repo / 'requirements.txt').write_text('django\npytest\n')
    language = onboard_env['engine'].detect_language(repo)
    framework = onboard_env['engine'].detect_framework(repo, language.name)
    template = onboard_env['engine'].select_template(repo, language, framework)
    assert template == 'django-app'


def test_detect_typescript_framework_react(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'tsconfig.json').write_text('{}')
    (repo / 'package.json').write_text(json.dumps({'dependencies': {'react': '^18.0.0', 'typescript': '^4.0.0'}}))
    framework = onboard_env['engine'].detect_framework(repo, 'typescript')
    assert framework == 'react'


def test_select_plugin_for_test_language(onboard_env):
    assert onboard_env['engine'].select_plugin('test') == 'plugin-test'


def test_full_onboard_test_repo(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'test.txt').write_text('test content')
    result = onboard_env['engine'].onboard(
        repo,
        OnboardOptions(name='test-onboard-repo', language='test', capture_baseline=True),
    )
    assert result.repo is not None
    assert result.repo.config.name == 'test-onboard-repo'
    assert result.plugin_id == 'plugin-test'
    assert result.health is not None
    assert result.findings_count > 0
    assert isinstance(result.review_items, list)

    loaded = onboard_env['registry'].read('test-onboard-repo')
    assert loaded is not None
    assert loaded.status == RepoStatus.READY


def test_generate_config_infers_backend_and_discovery(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'package.json').write_text(json.dumps({'scripts': {'test': 'vitest', 'build': 'tsc -p .'}}))
    (repo / 'Dockerfile').write_text('FROM node:20')
    language = onboard_env['engine'].detect_language(repo)
    config = onboard_env['engine'].generate_config(repo, 'smart-config', language, None, 'plugin-typescript')

    assert config.fix_engine in {'auto', 'claude', 'opencode', 'deterministic'}
    assert 'deterministic' in config.fallback_engines
    assert config.discovery.get('use_docker') is True
    assert any(cmd[:2] == ['npm', 'test'] for cmd in config.baseline_checks)
    assert config.meta['onboarding_version'] == 2


def test_select_template_for_react_repo(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'tsconfig.json').write_text('{}')
    (repo / 'package.json').write_text(json.dumps({'dependencies': {'react': '^18.0.0', 'typescript': '^5.0.0'}, 'scripts': {'test': 'vitest', 'build': 'vite build'}}))
    language = onboard_env['engine'].detect_language(repo)
    framework = onboard_env['engine'].detect_framework(repo, language.name)
    template = onboard_env['engine'].select_template(repo, language, framework)
    assert template == 'react-app'


def test_detect_monorepo_and_select_workspace_template(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'package.json').write_text(json.dumps({'workspaces': ['packages/*'], 'scripts': {'test': 'turbo test', 'build': 'turbo build'}, 'devDependencies': {'typescript': '^5.0.0'}}))
    language = onboard_env['engine'].detect_language(repo)
    monorepo = onboard_env['engine'].detect_monorepo(repo, language)
    template = onboard_env['engine'].select_template(repo, language, None)
    assert monorepo['is_monorepo'] is True
    assert template == 'node-workspace-root'


def test_explicit_template_override(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'tsconfig.json').write_text('{}')
    (repo / 'package.json').write_text(json.dumps({'dependencies': {'react': '^18.0.0', 'typescript': '^5.0.0'}}))
    result = onboard_env['engine'].onboard(
        repo,
        OnboardOptions(name='templated-repo', language='typescript', template='next-app', capture_baseline=False),
    )
    assert result.template == 'next-app'
    loaded = onboard_env['registry'].read('templated-repo')
    assert loaded.config.meta['template'] == 'next-app'
    assert loaded.config.meta['onboarding_version'] == 2


def test_python_baseline_checks_respect_poetry(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'pyproject.toml').write_text('[tool.poetry]\nname = "demo"\nversion = "0.1.0"\n')
    (repo / 'poetry.lock').write_text('# lock')
    (repo / 'tests').mkdir()
    language = onboard_env['engine'].detect_language(repo)
    checks = onboard_env['engine'].infer_baseline_checks(repo, language)
    assert ['poetry', 'run', 'pytest', '-q'] in checks


def test_safety_policy_inference_and_profile(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'package.json').write_text(json.dumps({'scripts': {'test': 'vitest'}}))
    language = onboard_env['engine'].detect_language(repo)
    config = onboard_env['engine'].generate_config(repo, 'safe-config', language, None, 'plugin-typescript')
    config.safety = onboard_env['engine'].infer_safety_policy(
        repo,
        OnboardOptions(mode='pr', profile='aggressive', allow_dirty_worktree=True),
    )
    config = onboard_env['engine'].apply_safety_profile(config)

    assert config.safety['mode'] == 'pr'
    assert config.safety['profile'] == 'aggressive'
    assert config.github['live_actions'] is True
    assert config.github['auto_merge'] is False
    assert config.limits['max_prs_per_run'] >= 3
    assert config.safety['allow_live_on_dirty_tree'] is True


def test_live_enabled_onboarding_blocks_dirty_worktree(onboard_env):
    repo = onboard_env['repo_path']
    (repo / '.git').mkdir()
    (repo / 'test.txt').write_text('test content')

    class FakeCompleted:
        def __init__(self, returncode=0, stdout=' M dirty.txt\n'):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ''

    import qa_agent.onboard as onboard_mod
    original = onboard_mod.subprocess.run if hasattr(onboard_mod, 'subprocess') else None
    import subprocess as real_subprocess
    onboard_mod.subprocess = real_subprocess
    saved_run = onboard_mod.subprocess.run
    onboard_mod.subprocess.run = lambda *args, **kwargs: FakeCompleted()
    try:
        with pytest.raises(ValueError):
            onboard_env['engine'].onboard(
                repo,
                OnboardOptions(name='dirty-live', language='test', mode='pr', profile='balanced', capture_baseline=False),
            )
    finally:
        onboard_mod.subprocess.run = saved_run


def test_onboard_already_exists(onboard_env):
    repo = onboard_env['repo_path']
    (repo / 'test.txt').write_text('test content')
    onboard_env['engine'].onboard(
        repo,
        OnboardOptions(name='dupe-repo', language='test', capture_baseline=False),
    )
    with pytest.raises(Exception):
        onboard_env['engine'].onboard(
            repo,
            OnboardOptions(name='dupe-repo', language='test', capture_baseline=False),
        )
