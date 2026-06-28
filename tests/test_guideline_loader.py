"""Tests for the authoritative guideline loader (ADR-0017 / Slice 3).

Guidelines are prompt-context files, NOT governed assets. These tests
verify: family matching, catch-all ("general"/"<language>"/"*") matching,
language filtering, no-match returns "", YAML round-trip, and prompt
section rendering.
"""

import textwrap
from pathlib import Path

import pytest

from bluei.engine.guideline_loader import load_authoritative_guidelines
from bluei.engine.prompts import render_claude_fix_prompt


# ── Test fixtures: a temp guidelines directory ─────────────────────────


@pytest.fixture
def guidelines_dir(tmp_path: Path) -> Path:
    """Write a representative set of guideline YAML files into tmp_path."""
    d = tmp_path / "guidelines"
    d.mkdir()

    (d / "pep8-naming.yaml").write_text(
        textwrap.dedent(
            """\
            topic: PEP 8 Naming Conventions
            language: python
            rule_families:
                - general
                - ruff-n
            directives:
                - "Functions and variables use snake_case."
                - "Classes use PascalCase."
            sources:
                - "PEP 8 § Naming Conventions"
            """
        ),
        encoding="utf-8",
    )

    (d / "owasp-input-validation.yaml").write_text(
        textwrap.dedent(
            """\
            topic: OWASP Input Validation Practices
            language: "*"
            rule_families:
                - general
            directives:
                - "Validate all external input at trust boundaries."
                - "Use parameterized queries for SQL."
            sources:
                - "OWASP Top 10 A03:2021 Injection"
            """
        ),
        encoding="utf-8",
    )

    (d / "typescript-conventions.yaml").write_text(
        textwrap.dedent(
            """\
            topic: TypeScript Conventions
            language: typescript
            rule_families:
                - general
                - eslint
            directives:
                - "Prefer interfaces over type aliases."
                - "Avoid any — use unknown."
            """
        ),
        encoding="utf-8",
    )

    # Language-specific catch-all that should NOT apply to other languages.
    (d / "rust-only.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Rust Specifics
            language: rust
            rule_families:
                - clippy
            directives:
                - "Prefer &str over String in function args."
            """
        ),
        encoding="utf-8",
    )

    return d


# ── Matching behavior ─────────────────────────────────────────────────


def test_family_match_python_ruff_n(guidelines_dir: Path):
    """ruff-n816 (family ruff-n) matches the PEP 8 file by family."""
    out = load_authoritative_guidelines("ruff-n816", "python", guidelines_dir)
    assert "PEP 8 Naming Conventions" in out
    assert "snake_case" in out


def test_general_catch_all_matches_any_rule(guidelines_dir: Path):
    """A rule with no specific family still gets 'general' catch-all files."""
    # 'xo-some-rule' derives to family 'xo' — not in any file, but 'general'
    # catch-all (OWASP language=*) applies for any language.
    out = load_authoritative_guidelines("xo-some-rule", "python", guidelines_dir)
    assert "OWASP Input Validation" in out
    assert "PEP 8 Naming Conventions" in out  # python general


def test_language_filter_excludes_other_languages(guidelines_dir: Path):
    """TypeScript-only file must NOT appear when language is python."""
    out = load_authoritative_guidelines("xo-foo", "python", guidelines_dir)
    assert "TypeScript Conventions" not in out


def test_language_star_applies_to_all_languages(guidelines_dir: Path):
    """OWASP file (language='*') applies to rust too."""
    out = load_authoritative_guidelines("clippy-needless", "rust", guidelines_dir)
    assert "OWASP Input Validation" in out
    # Rust-specific family file also matches.
    assert "Rust Specifics" in out


def test_no_match_returns_empty_string(guidelines_dir: Path):
    """A rule whose language+family hit nothing returns ''.

    Construct a directory with no general catch-all and a single family
    file in a different language.
    """
    d = guidelines_dir.parent / "only_ts"
    d.mkdir()
    (d / "ts.yaml").write_text(
        textwrap.dedent(
            """\
            topic: TypeScript Conventions
            language: typescript
            rule_families:
                - eslint
            directives:
                - "Avoid any."
            """
        ),
        encoding="utf-8",
    )
    # python rule, no general catch-all, no python-language file → no match
    assert load_authoritative_guidelines("eslint-no-undef", "python", d) == ""


def test_language_catch_all_family_entry(guidelines_dir: Path):
    """A file whose rule_families lists the bare language name applies."""
    d = guidelines_dir.parent / "lang_catchall"
    d.mkdir()
    (d / "py-file.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Python File Conventions
            language: python
            rule_families:
                - python
            directives:
                - "Use pathlib over os.path."
            """
        ),
        encoding="utf-8",
    )
    out = load_authoritative_guidelines("ruff-b904", "python", d)
    assert "Python File Conventions" in out


# ── Robustness ────────────────────────────────────────────────────────


def test_missing_directory_returns_empty(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    assert load_authoritative_guidelines("ruff-f401", "python", missing) == ""


def test_malformed_yaml_is_skipped(tmp_path: Path):
    d = tmp_path / "g"
    d.mkdir()
    (d / "broken.yaml").write_text("topic: [unterminated\n  bad: : :", encoding="utf-8")
    (d / "good.yaml").write_text(
        textwrap.dedent(
            """\
            topic: Good
            language: "*"
            rule_families: [general]
            directives: ["ok"]
            """
        ),
        encoding="utf-8",
    )
    out = load_authoritative_guidelines("any-rule", "python", d)
    assert "Good" in out
    assert "unterminated" not in out


def test_yaml_round_trip_via_safe_load(guidelines_dir: Path):
    """YAML files round-trip: directives appear verbatim in the output."""
    out = load_authoritative_guidelines(
        "eslint-no-unused-vars", "typescript", guidelines_dir
    )
    assert "Prefer interfaces over type aliases." in out
    assert "Avoid any — use unknown." in out


# ── Prompt section rendering ──────────────────────────────────────────


def test_render_claude_fix_prompt_includes_guidelines_section(make_finding):
    guidelines = (
        "### PEP 8 Naming Conventions\n"
        "- Functions and variables use snake_case.\n"
        "- Classes use PascalCase."
    )
    prompt = render_claude_fix_prompt(
        finding=make_finding(rule="ruff-n816", path="src/app.py"),
        baseline_checks={"pytest": ["pytest"]},
        target_checks={},
        max_files_changed=3,
        max_loc_diff=100,
        authoritative_guidelines=guidelines,
    )
    assert "## Authoritative guidelines" in prompt
    assert "snake_case" in prompt
    assert "Classes use PascalCase" in prompt


def test_render_claude_fix_prompt_omits_section_when_none(make_finding):
    prompt = render_claude_fix_prompt(
        finding=make_finding(rule="ruff-n816", path="src/app.py"),
        baseline_checks={"pytest": ["pytest"]},
        target_checks={},
        max_files_changed=3,
        max_loc_diff=100,
        authoritative_guidelines=None,
    )
    assert "## Authoritative guidelines" not in prompt


def test_guidelines_section_after_failure_clusters(make_finding):
    """Guidelines section renders AFTER the failure-clusters section."""
    prompt = render_claude_fix_prompt(
        finding=make_finding(rule="ruff-n816", path="src/app.py"),
        baseline_checks={},
        target_checks={},
        max_files_changed=3,
        max_loc_diff=100,
        failure_clusters="cluster-A",
        authoritative_guidelines="### X\n- rule one",
    )
    fc_pos = prompt.index("## Failure patterns for this rule")
    gl_pos = prompt.index("## Authoritative guidelines")
    assert gl_pos > fc_pos


# ── Governance isolation ──────────────────────────────────────────────


def test_loader_has_no_governance_dependency():
    """The loader module must not import any Operator Control Plane / learn
    namespace symbols. Guidelines are prompt-context, not governed assets
    (ADR-0017). Checked via the module's import AST, not docstring prose."""
    import ast
    import inspect

    import bluei.engine.guideline_loader as gl

    tree = ast.parse(inspect.getsource(gl))
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
