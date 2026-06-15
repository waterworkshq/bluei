#!/usr/bin/env python3
"""Pytest-native tests for Onboarding Engine."""

from pathlib import Path
import json

import pytest

from bluei.app.models import RepoStatus
from bluei.app.config import ConfigManager
from bluei.app.registry import RepoRegistry
from bluei.app.health import HealthEngine
from bluei.app.state import StateManager
from bluei.app.plugins import PluginLoader
from bluei.app.onboarding import OnboardEngine, OnboardOptions


@pytest.fixture
def onboard_env(tmp_path):
    workspace = tmp_path / "qa-agent"
    (workspace / "repos").mkdir(parents=True)
    (workspace / "plugins").mkdir()

    repo_path = tmp_path / "repo-under-test"
    repo_path.mkdir()

    config = ConfigManager(workspace)
    registry = RepoRegistry(config)
    health = HealthEngine()
    state = StateManager(config.repos_dir)
    source_plugins_dir = Path(__file__).resolve().parents[1] / "plugins"
    for plugin_dir in source_plugins_dir.iterdir():
        if plugin_dir.is_dir():
            target = workspace / "plugins" / plugin_dir.name
            target.mkdir(parents=True, exist_ok=True)
            for child in plugin_dir.iterdir():
                target_child = target / child.name
                if child.is_file():
                    target_child.write_text(child.read_text())
    source_templates_dir = Path(__file__).resolve().parents[1] / "templates" / "repos"
    target_templates_dir = workspace / "templates" / "repos"
    target_templates_dir.mkdir(parents=True, exist_ok=True)
    if source_templates_dir.exists():
        for template_file in source_templates_dir.glob("*.yaml"):
            (target_templates_dir / template_file.name).write_text(
                template_file.read_text()
            )
    plugins = PluginLoader(config.plugins_dir)
    engine = OnboardEngine(registry, plugins, health, state)

    return {
        "workspace": workspace,
        "repo_path": repo_path,
        "config": config,
        "registry": registry,
        "health": health,
        "state": state,
        "plugins": plugins,
        "engine": engine,
    }


def test_detect_python_language(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "setup.py").write_text("# setup")
    (repo / "requirements.txt").write_text("flask")
    lang = onboard_env["engine"].detect_language(repo)
    assert lang.name == "python"


def test_detect_typescript_language(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "tsconfig.json").write_text("{}")
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"typescript": "^4.0.0"}})
    )
    lang = onboard_env["engine"].detect_language(repo)
    assert lang.name == "typescript"


def test_detect_javascript_language(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"express": "^4.0.0"}})
    )
    lang = onboard_env["engine"].detect_language(repo)
    assert lang.name == "javascript"


def test_detect_go_language(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "go.mod").write_text("module test\n\ngo 1.21")
    lang = onboard_env["engine"].detect_language(repo)
    assert lang.name == "go"


def test_select_template_for_go_repo(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "go.mod").write_text("module test\n\ngo 1.21")
    lang = onboard_env["engine"].detect_language(repo)
    template = onboard_env["engine"].select_template(repo, lang, None)
    assert template == "go-service"


def test_detect_rust_language(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "Cargo.toml").write_text('[package]\nname = "test"')
    lang = onboard_env["engine"].detect_language(repo)
    assert lang.name == "rust"


def test_select_template_for_rust_repo(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "Cargo.toml").write_text('[package]\nname = "test"')
    lang = onboard_env["engine"].detect_language(repo)
    template = onboard_env["engine"].select_template(repo, lang, None)
    assert template == "rust-crate"


def test_detect_unknown_language(onboard_env):
    lang = onboard_env["engine"].detect_language(onboard_env["repo_path"])
    assert lang.name == "unknown"


def test_detect_python_framework_django(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "manage.py").write_text("# manage")
    (repo / "requirements.txt").write_text("django")
    framework = onboard_env["engine"].detect_framework(repo, "python")
    assert framework == "django"


def test_select_template_for_python_django_repo(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "manage.py").write_text("# manage")
    (repo / "requirements.txt").write_text("django\npytest\n")
    language = onboard_env["engine"].detect_language(repo)
    framework = onboard_env["engine"].detect_framework(repo, language.name)
    template = onboard_env["engine"].select_template(repo, language, framework)
    assert template == "django-app"


def test_detect_typescript_framework_react(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "tsconfig.json").write_text("{}")
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.0.0", "typescript": "^4.0.0"}})
    )
    framework = onboard_env["engine"].detect_framework(repo, "typescript")
    assert framework == "react"


def test_select_plugin_for_test_language(onboard_env):
    assert onboard_env["engine"].select_plugin("test") == "plugin-test"


def test_full_onboard_test_repo(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "test.txt").write_text("test content")
    result = onboard_env["engine"].onboard(
        repo,
        OnboardOptions(
            name="test-onboard-repo", language="test", capture_baseline=True
        ),
    )
    assert result.repo is not None
    assert result.repo.config.name == "test-onboard-repo"
    assert result.plugin_id == "plugin-test"
    assert result.health is not None
    assert result.findings_count > 0
    assert isinstance(result.review_items, list)

    loaded = onboard_env["registry"].read("test-onboard-repo")
    assert loaded is not None
    assert loaded.status == RepoStatus.READY


def test_generate_config_infers_backend_and_discovery(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "build": "tsc -p ."}})
    )
    (repo / "Dockerfile").write_text("FROM node:20")
    language = onboard_env["engine"].detect_language(repo)
    config = onboard_env["engine"].generate_config(
        repo, "smart-config", language, None, "plugin-typescript"
    )

    assert config.fix_engine in {"auto", "claude", "opencode", "deterministic"}
    assert "deterministic" in config.fallback_engines
    assert config.discovery.get("use_docker") is True
    assert any(cmd[:2] == ["npm", "test"] for cmd in config.baseline_checks)
    assert config.meta["onboarding_version"] == 2


def test_select_template_for_react_repo(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "tsconfig.json").write_text("{}")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"react": "^18.0.0", "typescript": "^5.0.0"},
                "scripts": {"test": "vitest", "build": "vite build"},
            }
        )
    )
    language = onboard_env["engine"].detect_language(repo)
    framework = onboard_env["engine"].detect_framework(repo, language.name)
    template = onboard_env["engine"].select_template(repo, language, framework)
    assert template == "react-app"


def test_detect_monorepo_and_select_workspace_template(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(
        json.dumps(
            {
                "workspaces": ["packages/*"],
                "scripts": {"test": "turbo test", "build": "turbo build"},
                "devDependencies": {"typescript": "^5.0.0"},
            }
        )
    )
    language = onboard_env["engine"].detect_language(repo)
    monorepo = onboard_env["engine"].detect_monorepo(repo, language)
    template = onboard_env["engine"].select_template(repo, language, None)
    assert monorepo["is_monorepo"] is True
    assert template == "node-workspace-root"


def test_explicit_template_override(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "tsconfig.json").write_text("{}")
    (repo / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^18.0.0", "typescript": "^5.0.0"}})
    )
    result = onboard_env["engine"].onboard(
        repo,
        OnboardOptions(
            name="templated-repo",
            language="typescript",
            template="next-app",
            capture_baseline=False,
        ),
    )
    assert result.template == "next-app"
    loaded = onboard_env["registry"].read("templated-repo")
    assert loaded.config.meta["template"] == "next-app"
    assert loaded.config.meta["onboarding_version"] == 2


def test_python_baseline_checks_respect_poetry(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "demo"\nversion = "0.1.0"\n'
    )
    (repo / "poetry.lock").write_text("# lock")
    (repo / "tests").mkdir()
    language = onboard_env["engine"].detect_language(repo)
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["poetry", "run", "pytest", "-q"] in checks


def test_safety_policy_inference_and_profile(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    language = onboard_env["engine"].detect_language(repo)
    config = onboard_env["engine"].generate_config(
        repo, "safe-config", language, None, "plugin-typescript"
    )
    config.safety = onboard_env["engine"].infer_safety_policy(
        repo,
        OnboardOptions(mode="pr", profile="aggressive", allow_dirty_worktree=True),
    )
    config = onboard_env["engine"].apply_safety_profile(config)

    assert config.safety["mode"] == "pr"
    assert config.safety["profile"] == "aggressive"
    assert config.github["live_actions"] is True
    assert config.github["auto_merge"] is False
    assert config.limits["max_prs_per_run"] >= 3
    assert config.safety["allow_live_on_dirty_tree"] is True


def test_live_enabled_onboarding_blocks_dirty_worktree(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / ".git").mkdir()
    (repo / "test.txt").write_text("test content")

    class FakeCompleted:
        def __init__(self, returncode=0, stdout=" M dirty.txt\n"):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    import bluei.app.onboarding as onboard_mod

    original = (
        onboard_mod.subprocess.run if hasattr(onboard_mod, "subprocess") else None
    )
    import subprocess as real_subprocess

    onboard_mod.subprocess = real_subprocess
    saved_run = onboard_mod.subprocess.run
    onboard_mod.subprocess.run = lambda *args, **kwargs: FakeCompleted()
    try:
        with pytest.raises(ValueError):
            onboard_env["engine"].onboard(
                repo,
                OnboardOptions(
                    name="dirty-live",
                    language="test",
                    mode="pr",
                    profile="balanced",
                    capture_baseline=False,
                ),
            )
    finally:
        onboard_mod.subprocess.run = saved_run


def test_onboard_already_exists(onboard_env):
    repo = onboard_env["repo_path"]
    (repo / "test.txt").write_text("test content")
    onboard_env["engine"].onboard(
        repo,
        OnboardOptions(name="dupe-repo", language="test", capture_baseline=False),
    )
    with pytest.raises(Exception):
        onboard_env["engine"].onboard(
            repo,
            OnboardOptions(name="dupe-repo", language="test", capture_baseline=False),
        )


def test_generate_config_with_fix_engine_override(onboard_env):
    """E7: fix_engine_override should set the fix engine in the config."""
    repo = onboard_env["repo_path"]
    (repo / "main.py").write_text('print("hello")\n')
    language = onboard_env["engine"].detect_language(repo)

    # Without override — auto-detected
    config_auto = onboard_env["engine"].generate_config(
        repo, "test", language, None, "plugin-python"
    )
    assert config_auto.fix_engine in {"auto", "claude", "opencode", "deterministic"}

    # With override — should use the override
    config_override = onboard_env["engine"].generate_config(
        repo, "test", language, None, "plugin-python", fix_engine_override="claude"
    )
    assert config_override.fix_engine == "claude"


def test_review_items_warn_when_plugin_requires_container_without_docker(onboard_env):
    """E4: requires_container plugin without docker should produce a review item."""
    from unittest.mock import patch

    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    language = onboard_env["engine"].detect_language(repo)
    config = onboard_env["engine"].generate_config(
        repo, "ts-test", language, None, "plugin-typescript"
    )

    # Simulate docker not being available
    with patch("shutil.which", return_value=None):
        items = onboard_env["engine"].build_review_items(repo, language, config)

    container_warnings = [
        i
        for i in items
        if "container" in i.message.lower() and "docker" in i.message.lower()
    ]
    assert len(container_warnings) > 0, f"Expected container warning in: {items}"
    # E6: requires-container-without-docker is a warn-severity tool item
    assert all(
        i.severity == "warn" and i.category == "tool" for i in container_warnings
    )


def test_review_items_no_warning_when_docker_available(onboard_env):
    """E4: requires_container plugin WITH docker should note docker was found."""
    from unittest.mock import patch

    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    language = onboard_env["engine"].detect_language(repo)
    config = onboard_env["engine"].generate_config(
        repo, "ts-test", language, None, "plugin-typescript"
    )

    with patch("shutil.which", return_value="/usr/bin/docker"):
        items = onboard_env["engine"].build_review_items(repo, language, config)

    docker_notes = [i for i in items if "docker found" in i.message.lower()]
    assert len(docker_notes) > 0, f"Expected docker-found note in: {items}"


# ── E3: plugin baseline_checks fallback ────────────────────────


def test_plugin_baseline_fallback_python(onboard_env):
    """E3: when per-language inference is empty, fall back to the python
    plugin's validation.baseline_checks (which declares pytest)."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    # Bare repo — no setup.py, no pyproject.toml, no tests/ — inference empty
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["python3", "-m", "pytest", "-q"] in checks


def test_plugin_baseline_fallback_typescript(onboard_env):
    """E3: typescript plugin declares npm test + npm run lint."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    language = LanguageInfo(name="typescript")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["npm", "test"] in checks
    assert ["npm", "run", "lint"] in checks


def test_plugin_baseline_fallback_skipped_when_inference_succeeds(onboard_env):
    """E3: plugin fallback is NOT used when inference returns commands."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "go.mod").write_text("module demo\n\ngo 1.21\n")
    language = LanguageInfo(name="go")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["go", "test", "./..."] in checks


# ── E2: smarter build-system inference ─────────────────────────


def test_makefile_test_target_detected(onboard_env):
    """E2: Makefile with a `test` target yields `["make", "test"]`."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "Makefile").write_text(
        "test:\n\tpytest -q\n\nlint:\n\truff check .\n\ncheck:\n\tmypy .\n"
    )
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["make", "test"] in checks
    assert ["make", "lint"] in checks
    assert ["make", "check"] in checks


def test_makefile_no_test_target_no_command(onboard_env):
    """E2: Makefile without test/lint/check yields no make commands."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "Makefile").write_text("deploy:\n\techo hi\n")
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert not any(cmd and cmd[0] == "make" for cmd in checks)


def test_tox_ini_detected(onboard_env):
    """E2: tox.ini presence yields `["tox"]`."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "tox.ini").write_text("[tox]\nenvlist = py39,py310\n")
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["tox"] in checks


def test_noxfile_detected(onboard_env):
    """E2: noxfile.py presence yields `["nox"]`."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "noxfile.py").write_text(
        "import nox\n\n@nox.session\ndef tests(s):\n    pass\n"
    )
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["nox"] in checks


def test_justfile_test_recipe_detected(onboard_env):
    """E2: justfile with a test recipe yields `["just", "test"]`."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "justfile").write_text("test:\n    pytest -q\n\nlint:\n    ruff check .\n")
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["just", "test"] in checks
    assert ["just", "lint"] in checks


def test_taskfile_test_task_detected(onboard_env):
    """E2: Taskfile.yml with a `test` task yields `["task", "test"]`."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "Taskfile.yml").write_text(
        "version: '3'\ntasks:\n  test:\n    cmds:\n      - pytest -q\n  lint:\n    cmds:\n      - ruff check .\n"
    )
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["task", "test"] in checks
    assert ["task", "lint"] in checks


def test_github_workflow_test_job_extracted(onboard_env):
    """E2: .github/workflows/ci.yml with a `test` job extracts run commands."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "name: CI\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: pytest -q\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make build\n"
        "  deploy:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: kubectl apply -f k8s\n"
    )
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["pytest", "-q"] in checks
    assert ["make", "build"] in checks
    # 'deploy' job should NOT contribute (no test/lint/build/check keyword)
    assert ["kubectl", "apply", "-f", "k8s"] not in checks


def test_github_workflow_multiline_run_picks_test_command(onboard_env):
    """E2: multi-line `run:` blocks should prefer the test/lint line."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          pip install -e .\n"
        "          pytest -q\n"
    )
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    assert ["pytest", "-q"] in checks
    assert ["pip", "install", "-e", "."] not in checks


def test_existing_package_json_detection_unchanged(onboard_env):
    """E2: existing npm-script detection must keep working."""
    repo = onboard_env["repo_path"]
    (repo / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "build": "tsc -p ."}})
    )
    language = onboard_env["engine"].detect_language(repo)
    config = onboard_env["engine"].generate_config(
        repo, "smart-config", language, None, "plugin-typescript"
    )
    assert any(cmd[:2] == ["npm", "test"] for cmd in config.baseline_checks)


def test_build_system_checks_merged_with_language_checks(onboard_env):
    """E2: build-system commands merge with (and dedupe against) language checks."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    (repo / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "demo"\nversion = "0.1.0"\n'
    )
    (repo / "poetry.lock").write_text("# lock\n")
    (repo / "tests").mkdir()
    (repo / "Makefile").write_text("test:\n\tpytest -q\nlint:\n\truff check .\n")
    language = LanguageInfo(name="python")
    checks = onboard_env["engine"].infer_baseline_checks(repo, language)
    # Poetry-detected
    assert ["poetry", "run", "pytest", "-q"] in checks
    # Makefile-detected
    assert ["make", "test"] in checks
    assert ["make", "lint"] in checks


# ── E1: PreflightResult data flow into onboard() ──────────────


def test_onboard_with_preflight_result_skips_detect_language(onboard_env):
    """E1: when preflight_result is supplied, detect_language() must not run."""
    from unittest.mock import patch
    from bluei.app.preflight import PreflightResult

    repo = onboard_env["repo_path"]
    (repo / "main.py").write_text("print('hello')\n")

    preflight = PreflightResult(
        repo_path=str(repo),
        language="python",
        secondary_languages=[],
        plugin_id="plugin-python",
    )
    options = OnboardOptions(
        name="preflight-skipped",
        preflight_result=preflight,
        capture_baseline=False,
    )

    with (
        patch.object(onboard_env["engine"], "detect_language") as mock_detect,
        patch.object(onboard_env["engine"], "select_plugin") as mock_select_plugin,
        patch("bluei.app.preflight.run_preflight") as mock_run_preflight,
    ):
        result = onboard_env["engine"].onboard(repo, options)

    # detect_language() must NOT have been called — preflight supplied the language
    assert not mock_detect.called, "detect_language should be skipped"
    # select_plugin() must NOT have been called — preflight supplied plugin_id
    assert not mock_select_plugin.called, "select_plugin should be skipped"
    # Inline preflight gate must NOT have been triggered
    assert not mock_run_preflight.called, "run_preflight gate should be skipped"

    # Language comes from preflight
    assert result.language.name == "python"
    assert result.plugin_id == "plugin-python"


def test_onboard_without_preflight_result_runs_detect_language(onboard_env):
    """E1: without preflight_result, the original detect_language path runs."""
    repo = onboard_env["repo_path"]
    (repo / "main.py").write_text("print('hello')\n")

    options = OnboardOptions(
        name="no-preflight",
        language="python",  # avoid relying on real detection
        capture_baseline=False,
        skip_preflight=True,  # avoid docker/tool dependency in this unit test
    )

    # Should NOT raise — preflight_result is None, so detection runs but
    # options.language override short-circuits the language assignment.
    result = onboard_env["engine"].onboard(repo, options)
    assert result.language.name == "python"


def test_onboard_preflight_result_provides_plugin_id(onboard_env):
    """E1: preflight_result.plugin_id is used when options.plugin_id is None."""
    from unittest.mock import patch
    from bluei.app.preflight import PreflightResult

    repo = onboard_env["repo_path"]
    (repo / "main.py").write_text("print('hello')\n")

    preflight = PreflightResult(
        repo_path=str(repo),
        language="python",
        secondary_languages=[],
        plugin_id="plugin-python",
    )
    options = OnboardOptions(
        name="preflight-plugin",
        preflight_result=preflight,
        capture_baseline=False,
    )

    with patch.object(onboard_env["engine"], "select_plugin") as mock_select_plugin:
        result = onboard_env["engine"].onboard(repo, options)

    assert not mock_select_plugin.called
    assert result.plugin_id == "plugin-python"


# ── E6: ReviewItem typed review items ──────────────────────────


def test_review_item_to_string_format():
    """E6: ReviewItem.to_string() renders '[severity] message'."""
    from bluei.app.onboarding.engine import ReviewItem

    item = ReviewItem(message="hello world", severity="warn")
    assert item.to_string() == "[warn] hello world"

    item_info = ReviewItem(message="all good")
    assert item_info.to_string() == "[info] all good"
    assert item_info.severity == "info"
    assert item_info.category == "config"
    assert item_info.suggested_action is None


def test_review_item_str_returns_message():
    """E6: str(ReviewItem) returns just the message (not the bracketed form)."""
    from bluei.app.onboarding.engine import ReviewItem

    item = ReviewItem(message="clean working tree required", severity="info")
    assert str(item) == "clean working tree required"


def test_build_review_items_returns_typed_objects(onboard_env):
    """E6: build_review_items returns ReviewItem instances with severity/category."""
    from bluei.app.onboarding.engine import ReviewItem
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    # Bare repo with no inferred baseline → "no baseline" warn item expected
    language = LanguageInfo(name="python")
    config = onboard_env["engine"].generate_config(
        repo, "typed-items", language, None, "plugin-python"
    )

    items = onboard_env["engine"].build_review_items(repo, language, config)
    assert all(isinstance(i, ReviewItem) for i in items), items

    # Severity values are constrained to the documented set
    valid_severities = {"info", "warn", "action-required"}
    valid_categories = {"safety", "config", "tool"}
    for i in items:
        assert i.severity in valid_severities, i
        assert i.category in valid_categories, i


def test_build_review_items_no_baseline_is_warn_config(onboard_env):
    """E6: 'No baseline validation commands' maps to severity=warn, category=config."""
    from bluei.app.models import LanguageInfo

    repo = onboard_env["repo_path"]
    language = LanguageInfo(name="python")
    config = onboard_env["engine"].generate_config(
        repo, "no-baseline", language, None, "plugin-python"
    )
    # Force empty baseline to trigger the warn item
    config.baseline_checks = []

    items = onboard_env["engine"].build_review_items(repo, language, config)
    no_baseline = next((i for i in items if "no baseline" in i.message.lower()), None)
    assert no_baseline is not None, items
    assert no_baseline.severity == "warn"
    assert no_baseline.category == "config"


def test_build_review_items_observe_mode_is_info_safety(onboard_env):
    """E6: 'Observe mode is active' maps to severity=info, category=safety."""
    from bluei.app.models import LanguageInfo, SafetyMode

    repo = onboard_env["repo_path"]
    language = LanguageInfo(name="python")
    config = onboard_env["engine"].generate_config(
        repo, "observe-mode", language, None, "plugin-python"
    )
    config.safety["mode"] = SafetyMode.OBSERVE.value

    items = onboard_env["engine"].build_review_items(repo, language, config)
    observe_item = next((i for i in items if "observe mode" in i.message.lower()), None)
    assert observe_item is not None, items
    assert observe_item.severity == "info"
    assert observe_item.category == "safety"
