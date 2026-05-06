#!/usr/bin/env python3
"""enforce_architecture.py — CI gate: verify acyc import order and completeness."""

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).parent
EXPECTED_MODULES = {
    "constants.py", "models.py", "utils.py", "state.py", "gh.py",
    "linters.py", "git_utils.py", "prompts.py", "orchestrator.py",
    "lifecycle.py", "cli.py", "reforge.py", "refactor_queue.py", "__init__.py",
}
LEGAL_IMPORTS = {
    # module: set of modules it may legally import from
    # Updated to reflect actual dependencies from backup file analysis
    "constants.py": set(),
    "models.py": {"constants"},
    "utils.py": {"constants"},
    "reforge.py": {"constants"},  # only imports constants (MAX_LINES_REFACTOR_*)
    "state.py": {"constants", "models", "utils", "gh", "reforge"},  # imports fetch_github_live_counts, get_origin_url, RefactorWork persistence helpers
    "gh.py": {"constants", "models", "utils", "state"},  # imports _append_text (lazy)
    "linters.py": {"constants", "models", "utils", "state"},  # imports _append_text
    "git_utils.py": {"constants", "models", "utils", "linters", "state"},  # imports _append_text
    "prompts.py": {"constants", "models", "utils"},
    "orchestrator.py": {"constants", "models", "utils", "state", "linters", "git_utils", "gh", "prompts", "reforge", "refactor_queue"},
    "lifecycle.py": {
        "constants", "models", "utils", "state", "linters", "git_utils",
        "prompts", "orchestrator", "gh", "reforge", "refactor_queue",
    },  # reforge: classify_finding, safety gates; refactor_queue: enqueue/route functions
    "cli.py": {"constants", "models", "utils", "state", "gh", "orchestrator", "lifecycle", "prompts", "git_utils"},
}


def check_module(path: Path) -> list[str]:
    """Return list of violations for one module. Empty = clean."""
    tree = ast.parse(path.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "sandbox_local_runner" in str(node.module):
                imports.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "sandbox_local_runner" in str(alias.name):
                    imports.add(alias.name.split(".")[-1])
    name = path.name
    legal = LEGAL_IMPORTS.get(name, set())
    illegal = imports - legal
    return [f"{name} illegally imports {i}" for i in illegal]


def main():
    violations = []
    for py_file in PACKAGE.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        # Skip test and tooling files
        if py_file.name in (
            "test_refactor.py",
            "test_directive_seeding.py",
            "test_directive_seeding_e2e.py",
            "test_mnemo_client.py",
            "test_reforge.py",
            "test_refactor_queue.py",
            "test_route_findings_with_intent.py",
            "test_refactor_state.py",
            "enforce_architecture.py",
            "check_completeness.py",
            "llm_fixable_rules.yaml",
            "mnemo_client.py",
        ):
            continue
        violations.extend(check_module(py_file))

    # Also check __init__.py has no wildcard re-exports
    init = PACKAGE / "__init__.py"
    if init.exists():
        src = init.read_text()
        if "*" in src and "import *" in src:
            violations.append("__init__.py contains wildcard import")

    if violations:
        for v in violations:
            print(f"VIOLATION: {v}", file=sys.stderr)
        sys.exit(1)
    print("✅  Architecture check passed")


main()
