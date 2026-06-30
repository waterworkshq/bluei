"""Tests for the repo taste loader (ADR-0017 principle / Slice 0 — Taste Atlas).

Taste entries are prompt-context files, NOT governed assets. These tests
verify: framework matching, language filtering, catch-all ``*``, exception
``rule_families`` scoping, empty-when-no-match, malformed-YAML skip, plus
the §6f prompt section rendering and the RunContext.repo_config default.
"""

import textwrap
from argparse import Namespace
from pathlib import Path

import pytest

from bluei.engine.repo_taste_loader import load_repo_taste
from bluei.engine.prompts import render_claude_fix_prompt
from bluei.engine.commands.context import RunContext


# ── Test fixtures: a temp taste directory ────────────────────────────


@pytest.fixture
def taste_dir(tmp_path: Path) -> Path:
    """Write a representative set of taste YAML files into tmp_path."""
    d = tmp_path / "taste"
    d.mkdir()

    (d / "django.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Django conventions
            language: python
            frameworks:
                - django
            directives:
                - "Use class-based views for CRUD."
                - "Prefer model managers for querysets."
            """
        ),
        encoding="utf-8",
    )

    (d / "react.yaml").write_text(
        textwrap.dedent(
            """\
            topic: React conventions
            language: typescript
            frameworks:
                - react
            directives:
                - "Prefer function components."
            """
        ),
        encoding="utf-8",
    )

    # Catch-all: applies to any language via ``*``.
    (d / "generic.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Generic repo taste
            language: "*"
            frameworks:
                - "*"
            directives:
                - "Keep functions small."
            """
        ),
        encoding="utf-8",
    )

    # Language catch-all via bare language name in frameworks.
    (d / "python-all.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Python-wide conventions
            language: python
            frameworks:
                - python
            directives:
                - "Use pathlib over os.path."
            """
        ),
        encoding="utf-8",
    )

    # Exception entry: applies only via rule_family.
    (d / "ruff-b-exception.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Bugbear exception taste
            language: python
            frameworks: []
            rule_families:
                - ruff-b
            directives:
                - "Raise from for re-raised exceptions."
            """
        ),
        encoding="utf-8",
    )

    return d


# ── Matching behavior ────────────────────────────────────────────────


def test_framework_match_django(taste_dir: Path):
    """django.yaml matches framework='django' but not framework='flask'."""
    out = load_repo_taste("django", "python", taste_dir=taste_dir)
    assert "Django conventions" in out
    assert "Use class-based views for CRUD." in out

    out_flask = load_repo_taste("flask", "python", taste_dir=taste_dir)
    assert "Django conventions" not in out_flask


def test_language_filter_excludes_other_languages(taste_dir: Path):
    """react.yaml (language=typescript) must NOT appear for python queries."""
    out = load_repo_taste("react", "python", taste_dir=taste_dir)
    assert "React conventions" not in out


def test_language_star_catch_all(taste_dir: Path):
    """generic.yaml (frameworks=['*'], language='*') applies everywhere."""
    out = load_repo_taste("flask", "python", taste_dir=taste_dir)
    assert "Generic repo taste" in out
    assert "Keep functions small." in out


def test_language_name_catch_all_in_frameworks(taste_dir: Path):
    """python-all.yaml lists bare 'python' in frameworks — applies for python."""
    out = load_repo_taste("flask", "python", taste_dir=taste_dir)
    assert "Python-wide conventions" in out


def test_exception_rule_family_scopes_correctly(taste_dir: Path):
    """Exception entry with rule_families=[ruff-b] applies only when matched."""
    out_match = load_repo_taste(
        "django", "python", rule_family="ruff-b", taste_dir=taste_dir
    )
    assert "Bugbear exception taste" in out_match

    out_other = load_repo_taste(
        "django", "python", rule_family="ruff-f", taste_dir=taste_dir
    )
    assert "Bugbear exception taste" not in out_other


def test_no_match_returns_empty_string(taste_dir: Path):
    """typescript-only file with no catch-all returns '' for python queries."""
    d = taste_dir.parent / "ts_only"
    d.mkdir()
    (d / "ts.yaml").write_text(
        textwrap.dedent(
            """\
            topic: TS-only
            language: typescript
            frameworks:
                - react
            directives:
                - "ts only directive."
            """
        ),
        encoding="utf-8",
    )
    assert load_repo_taste("react", "python", taste_dir=d) == ""


def test_missing_directory_returns_empty(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert load_repo_taste("django", "python", taste_dir=missing) == ""


def test_malformed_yaml_is_skipped(tmp_path: Path):
    """Malformed YAML is skipped silently; good files still match."""
    d = tmp_path / "t"
    d.mkdir()
    (d / "broken.yaml").write_text("topic: [unterminated\n  bad: : :", encoding="utf-8")
    (d / "good.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Good
            language: "*"
            frameworks: ["*"]
            directives: ["ok"]
            """
        ),
        encoding="utf-8",
    )
    out = load_repo_taste("any-fw", "python", taste_dir=d)
    assert "Good" in out
    assert "unterminated" not in out


def test_none_framework_with_no_catch_all_returns_empty(tmp_path: Path):
    """framework=None and no rule_family — only ``*``/language catch-alls apply."""
    d = tmp_path / "strict"
    d.mkdir()
    (d / "django.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Django
            language: python
            frameworks: [django]
            directives: ["django only"]
            """
        ),
        encoding="utf-8",
    )
    # No framework and no catch-all → no match
    assert load_repo_taste(None, "python", taste_dir=d) == ""


# ── Product seed fixtures round-trip ─────────────────────────────────


def test_seed_fixtures_round_trip():
    """The 3 shipped seed fixtures round-trip through the loader."""
    # Django
    out = load_repo_taste("django", "python")
    assert "Django conventions" in out
    assert "class-based views" in out
    # pytest
    out = load_repo_taste("pytest", "python")
    assert "pytest conventions" in out
    # react (typescript)
    out = load_repo_taste("react", "typescript")
    assert "React conventions" in out


# ── Prompt section 6f rendering ──────────────────────────────────────


def test_render_claude_fix_prompt_includes_taste_section(make_finding):
    taste = (
        "### Django conventions\n"
        "- Use class-based views for CRUD.\n"
        "- Prefer model managers for querysets."
    )
    prompt = render_claude_fix_prompt(
        finding=make_finding(rule="ruff-n816", path="src/app.py"),
        baseline_checks={"pytest": ["pytest"]},
        target_checks={},
        max_files_changed=3,
        max_loc_diff=100,
        repo_taste=taste,
    )
    assert "## Repo taste" in prompt
    assert "class-based views" in prompt


def test_render_claude_fix_prompt_omits_taste_section_when_none(make_finding):
    prompt = render_claude_fix_prompt(
        finding=make_finding(rule="ruff-n816", path="src/app.py"),
        baseline_checks={"pytest": ["pytest"]},
        target_checks={},
        max_files_changed=3,
        max_loc_diff=100,
        repo_taste=None,
    )
    assert "## Repo taste" not in prompt


def test_taste_section_after_guidelines_section(make_finding):
    """Taste (§6f) renders AFTER guidelines (§6e)."""
    prompt = render_claude_fix_prompt(
        finding=make_finding(rule="ruff-n816", path="src/app.py"),
        baseline_checks={},
        target_checks={},
        max_files_changed=3,
        max_loc_diff=100,
        authoritative_guidelines="### GL\n- gl directive",
        repo_taste="### TASTE\n- taste directive",
    )
    gl_pos = prompt.index("## Authoritative guidelines")
    taste_pos = prompt.index("## Repo taste")
    assert taste_pos > gl_pos


# ── Governance isolation ─────────────────────────────────────────────


def test_loader_has_no_governance_dependency():
    """The loader module must not import governed namespaces (ADR-0017)."""
    import ast
    import inspect

    import bluei.engine.repo_taste_loader as rtl

    tree = ast.parse(inspect.getsource(rtl))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_names.add(node.module)
    for forbidden in (
        "bluei.learn",
        "bluei.app.learn",
        "bluei.engine.operator_control",
        "bluei.governance",
    ):
        assert forbidden not in imported_names, (
            f"loader imports governed namespace: {forbidden}"
        )


# ── RunContext.repo_config default ───────────────────────────────────


def test_runcontext_repo_config_defaults_none():
    """RunContext.repo_config defaults to None (additive; populated at cycle start)."""
    ctx = RunContext(args=Namespace())
    assert ctx.repo_config is None
