#!/usr/bin/env python3
"""Tests for legacy path hygiene - ensures no production code references pr-automation."""

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_no_pr_automation_references_in_production_code():
    """Verify that production code does not reference legacy pr-automation paths."""
    excluded_dirs = {
        "tests",
        "scripts/retired",
        "repos",
        "logs",
        "__pycache__",
        ".git",
    }

    # Find all Python files in the project
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "pr-automation", str(PROJECT_ROOT)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        # No matches found - good!
        return

    # Parse matches and filter out excluded directories
    matches = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # Extract file path from grep output
        file_path = line.split(":")[0]
        rel_path = Path(file_path).relative_to(PROJECT_ROOT)

        # Check if in excluded directory
        parts = rel_path.parts
        is_excluded = False
        for i in range(len(parts)):
            if parts[i] in excluded_dirs or str(Path(*parts[: i + 1])) in excluded_dirs:
                is_excluded = True
                break

        if not is_excluded:
            matches.append(line)

    # Allow matches only in tests and retired scripts
    assert not matches, (
        f"Found pr-automation references in production code:\n"
        + "\n".join(matches)
        + "\n\nLegacy pr-automation paths should not be referenced in production code."
        + "\nIf this is intentional, add the file to excluded_dirs."
    )


def test_no_pr_automation_imports_in_qa_agent_module():
    """Verify qa_agent module does not import from pr-automation."""
    qa_agent_dir = PROJECT_ROOT / "qa_agent"

    for py_file in qa_agent_dir.rglob("*.py"):
        content = py_file.read_text()
        # Check for various forms of pr-automation references
        assert "pr-automation" not in content, f"Found 'pr-automation' in {py_file}"
        assert "pr_automation" not in content, f"Found 'pr_automation' in {py_file}"
        assert "prautomation" not in content.lower(), (
            f"Found prautomation variant in {py_file}"
        )


def test_no_pr_automation_imports_in_core_module():
    """Verify core module does not import from pr-automation."""
    core_dir = PROJECT_ROOT / "core"

    if not core_dir.exists():
        return  # core module may not exist in test environment

    for py_file in core_dir.rglob("*.py"):
        content = py_file.read_text()
        assert "pr-automation" not in content, f"Found 'pr-automation' in {py_file}"
        assert "pr_automation" not in content, f"Found 'pr_automation' in {py_file}"
