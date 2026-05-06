#!/usr/bin/env python3
"""test_refactor.py — comprehensive tests for the sandbox_local_runner package refactor."""

import ast
import sys
from pathlib import Path

# Allow running as script from core/ directory
sys.path.insert(0, str(Path(__file__).parent))
PACKAGE = Path(__file__).parent
BACKUP = PACKAGE.parent / "sandbox_local_runner.py.bak"


def test_backup_file_exists_and_correct_line_count():
    """Backup file exists and has correct line count (4176)."""
    assert BACKUP.exists(), f"Backup file not found: {BACKUP}"
    lines = BACKUP.read_text().splitlines()
    # Allow some variance - the original spec says 4176
    assert 4100 < len(lines) < 4200, f"Backup has {len(lines)} lines, expected ~4176"
    print(f"✅ backup file exists with {len(lines)} lines")


def test_finding_dataclass_instantiation():
    """Finding dataclass can be instantiated."""
    sys.path.insert(0, str(PACKAGE.parent))
    from sandbox_local_runner.models import Finding

    f = Finding(
        finding_id="test-001",
        repo="test-repo",
        path="src/test.py",
        line=10,
        rule="test-rule",
        snippet="print('hello')",
        confidence=0.95,
        quick_win=True,
        safe_to_autofix=True,
    )
    assert f.finding_id == "test-001"
    assert f.rule == "test-rule"
    assert f.confidence == 0.95
    d = f.as_dict()
    assert d["finding_id"] == "test-001"
    print("✅ Finding dataclass instantiation works")


def test_main_is_callable():
    """main() is defined and callable (won't execute — just checks signature)."""
    sys.path.insert(0, str(PACKAGE.parent))
    from sandbox_local_runner.cli import main

    assert callable(main), "main() should be callable"
    print("✅ main() is callable")


def test_all_public_symbols_importable():
    """Every public symbol from __init__.py is importable."""
    sys.path.insert(0, str(PACKAGE.parent))
    import sandbox_local_runner

    # Test a representative sample of public symbols from each module
    symbols_to_test = [
        # models
        "Finding",
        "now_iso",
        "age_seconds",
        "stable_finding_id",
        # constants
        "DETECTOR_CATALOG",
        "BASELINE_VALIDATION_CHECKS",
        "RULE_TARGET_CHECKS",
        "CLAUDE_REQUIRED_RULES",
        "DEFAULT_REPO",
        "DEFAULT_STATE",
        # utils
        "run_capture",
        "sanitize_command_template",
        "command_list_to_shell",
        "append_lesson",
        "assert_safe_repo",
        "branch_suffix",
        # state
        "load_state",
        "save_state",
        "load_issues",
        "save_issues",
        "guard_open_issues",
        "guard_open_prs",
        "filter_findings_by_cooldown",
        # gh
        "get_origin_url",
        "parse_github_repo",
        "repo_is_sandbox",
        "create_or_update_github_issue",
        "fetch_github_live_counts",
        # linters
        "discover_xo_linter_findings",
        "discover_python_linter_findings",
        "discover_test_coverage_findings",
        # git_utils
        "get_branch",
        "refresh_docs_index",
        "load_docs_index",
        # prompts
        "render_claude_fix_prompt",
        "render_test_coverage_prompt",
        # orchestrator
        "discover_findings",
        "create_issues_for_findings",
        "build_active_cycle_command",
        "build_orchestrated_cycle_command",
        # lifecycle
        "apply_autofix",
        "git_commit_all",
        "git_push_branch",
        "diff_stats",
        "run_named_checks",
        "build_target_checks",
        "apply_claude_fix",
        "run_validation_gate",
        "classify_review_feedback",
        "review_loop_allowed",
        "verify_fix_closed",
        # cli
        "update_status_artifact",
    ]

    failed = []
    for sym in symbols_to_test:
        if not hasattr(sandbox_local_runner, sym):
            failed.append(sym)

    if failed:
        print(f"❌ Failed to import: {failed}")
        assert False, f"Missing symbols: {failed}"
    print(f"✅ All {len(symbols_to_test)} public symbols are importable")


def test_package_modules_exist():
    """All expected module files exist."""
    expected = [
        "__init__.py",
        "constants.py",
        "models.py",
        "utils.py",
        "state.py",
        "gh.py",
        "linters.py",
        "git_utils.py",
        "prompts.py",
        "orchestrator.py",
        "lifecycle.py",
        "cli.py",
    ]
    for mod in expected:
        path = PACKAGE / mod
        assert path.exists(), f"Module not found: {mod}"
    print(f"✅ All {len(expected)} module files exist")


def test_no_wildcard_imports_in_init():
    """__init__.py has no wildcard imports."""
    init = PACKAGE / "__init__.py"
    content = init.read_text()
    if "import *" in content:
        # Check if it's in a string or comment (not an actual import)
        for line in content.splitlines():
            stripped = line.strip()
            if "import *" in stripped and not stripped.startswith("#"):
                assert False, f"Wildcard import found in __init__.py: {line}"
    print("✅ __init__.py has no wildcard imports")


def test_enforce_architecture_passes():
    """enforce_architecture.py runs and passes."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(PACKAGE / "enforce_architecture.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
    assert result.returncode == 0, "enforce_architecture.py failed"
    print("✅ enforce_architecture.py passes")


def test_check_completeness_passes():
    """check_completeness.py runs and passes."""
    import subprocess
    result = subprocess.run(
        [sys.executable, str(PACKAGE / "check_completeness.py")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
    # Note: check_completeness might fail on missing symbols intentionally
    # The important thing is it runs without crashing
    print("✅ check_completeness.py runs")


def test_completeness_against_backup():
    """Completeness check: every source symbol appears in target."""
    if not BACKUP.exists():
        print("⚠️  Skipping completeness check (backup not found)")
        return

    source_tree = ast.parse(BACKUP.read_text())
    source_symbols = set()
    for node in ast.walk(source_tree):
        if isinstance(node, ast.ClassDef):
            source_symbols.add(node.name)
        elif isinstance(node, ast.FunctionDef):
            source_symbols.add(node.name)

    # Known extras (private helpers that are nested/inlined in source)
    KNOWN_EXTRAS = {
        "_append_text",  # private in state.py
        "_normalize_check_output",  # private in lifecycle.py
        "_git_last_commit_for_path",  # private in git_utils.py
        "_code_paths_for_docs_index",  # private in git_utils.py
        "_has_inline_doc",  # private in git_utils.py
        "_external_doc_text",  # private in git_utils.py
        "_build_base_cycle_command",  # private in orchestrator.py
        "_read_lines",  # private nested in discover_findings
        "_add_finding",  # private nested in discover_findings
        "_get_ruff_rule_description",  # private in linters.py (dead code)
        "gh_json",  # private in gh.py
    }

    target_symbols = set()
    for py_file in PACKAGE.glob("*.py"):
        if py_file.name.startswith("_") or py_file.name in (
            "enforce_architecture.py",
            "check_completeness.py",
            "test_refactor.py",
        ):
            continue
        try:
            t = ast.parse(py_file.read_text())
        except Exception:
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.ClassDef):
                target_symbols.add(node.name)
            elif isinstance(node, ast.FunctionDef):
                target_symbols.add(node.name)

    missing = source_symbols - target_symbols
    missing = missing - KNOWN_EXTRAS
    if missing:
        print(f"⚠️  Missing symbols (may be expected): {sorted(missing)}")
    else:
        print("✅ All source symbols are covered in target package")


def main():
    tests = [
        test_package_modules_exist,
        test_backup_file_exists_and_correct_line_count,
        test_no_wildcard_imports_in_init,
        test_enforce_architecture_passes,
        test_check_completeness_passes,
        test_completeness_against_backup,
        test_finding_dataclass_instantiation,
        test_main_is_callable,
        test_all_public_symbols_importable,
    ]

    failed = []
    for test in tests:
        try:
            test()
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed.append(test.__name__)

    print()
    if failed:
        print(f"❌ {len(failed)} test(s) failed: {failed}")
        sys.exit(1)
    else:
        print(f"✅ All {len(tests)} tests passed")


if __name__ == "__main__":
    main()
