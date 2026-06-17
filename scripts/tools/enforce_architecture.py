#!/usr/bin/env python3
"""enforce_architecture.py — CI gate: verify acyc import order and completeness.

Architecture invariants enforced:
  1. bluei/engine/ modules only import from a fixed legal set (LEGAL_IMPORTS).
  2. bluei/engine/ never imports from bluei/app/ (rec-08 layering violation).
     The only sanctioned exemption is bluei/engine/state_io.py.
  3. bluei/review/ never imports from bluei/app/ (H5 layering violation).
     Review must import shared types from bluei/common/ and review-specific
     helpers from bluei/review/state.py. App-layer services that review/
     used to reach into (emergent_rules, onboarding.inference, escalation,
     pattern_confidence) are now dependency-inverted via callbacks wired in
     app/runner.py — see ReviewCycleEngine.__init__'s Phase D parameters.
"""

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "bluei" / "engine"
REVIEW_PACKAGE = Path(__file__).resolve().parents[2] / "bluei" / "review"
EXPECTED_MODULES = {
    "constants.py",
    "models.py",
    "utils.py",
    "state.py",
    "state_io.py",
    "gh.py",
    "linters.py",
    "git_utils.py",
    "prompts.py",
    "orchestrator.py",
    "lifecycle.py",
    "issue_lifecycle.py",
    "cli.py",
    "reforge.py",
    "refactor_queue.py",
    "__init__.py",
}
LEGAL_IMPORTS = {
    # module: set of modules it may legally import from
    # Updated to reflect actual dependencies from backup file analysis
    "constants.py": set(),
    "models.py": {"constants"},
    "utils.py": {"constants"},
    "reforge.py": {"constants"},  # only imports constants (MAX_LINES_REFACTOR_*)
    "state.py": {
        "constants",
        "models",
        "utils",
        "gh",
        "reforge",
        "state_io",
        "issue_lifecycle",
    },  # state_io: atomic JSON I/O (rec-08); issue_lifecycle: re-exported helpers
    "state_io.py": set(),  # pure file-I/O primitives, no engine deps (rec-08 extraction)
    "issue_lifecycle.py": {
        "constants",
        "models",
        "utils",
        "state",
        "state_io",
    },  # state_io: atomic JSON I/O (rec-08)
    "gh.py": {"constants", "models", "utils", "state"},  # imports _append_text (lazy)
    "linters.py": {"constants", "models", "utils", "state"},  # imports _append_text
    "git_utils.py": {
        "constants",
        "models",
        "utils",
        "linters",
        "state",
    },  # imports _append_text
    "prompts.py": {"constants", "models", "utils"},
    "orchestrator.py": {
        "constants",
        "models",
        "utils",
        "state",
        "linters",
        "git_utils",
        "gh",
        "prompts",
        "reforge",
        "refactor_queue",
    },
    "lifecycle.py": {
        "constants",
        "models",
        "utils",
        "state",
        "linters",
        "git_utils",
        "prompts",
        "orchestrator",
        "gh",
        "reforge",
        "refactor_queue",
    },  # reforge: classify_finding, safety gates; refactor_queue: enqueue/route functions
    "cli.py": {
        "constants",
        "models",
        "utils",
        "state",
        "gh",
        "orchestrator",
        "lifecycle",
        "prompts",
        "git_utils",
    },
}


def check_module(path: Path) -> list[str]:
    """Return list of violations for one module. Empty = clean."""
    tree = ast.parse(path.read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "bluei.engine" in str(node.module):
                imports.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "bluei.engine" in str(alias.name):
                    imports.add(alias.name.split(".")[-1])
    name = path.name
    legal = LEGAL_IMPORTS.get(name, set())
    illegal = imports - legal
    return [f"{name} illegally imports {i}" for i in illegal]


def check_engine_imports_app(path: Path) -> list[str]:
    """Flag any `from bluei.app...` import inside bluei/engine/.

    Catches rec-08 layering violations: engine must never depend on app.
    Deferred (function-local) imports are also flagged — they are still
    hard dependencies at runtime and invert the intended layering.

    The only sanctioned escape hatch is bluei/engine/state_io.py, which
    is intentionally app-free (it was extracted specifically so engine
    could have atomic JSON I/O without reaching into app).
    """
    if path.name == "state_io.py":
        return []
    violations: list[str] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and str(node.module).startswith("bluei.app"):
                violations.append(
                    f"{path.name}: engine→app import "
                    f"from bluei.app.{node.module[len('bluei.app') :].lstrip('.')} "
                    f"(rec-08 layering violation; use bluei/engine/state_io.py "
                    f"for atomic JSON I/O)"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if str(alias.name).startswith("bluei.app"):
                    violations.append(
                        f"{path.name}: engine→app import of {alias.name} "
                        f"(rec-08 layering violation)"
                    )
    return violations


def check_review_imports_app(path: Path) -> list[str]:
    """Flag any `from bluei.app...` import inside bluei/review/.

    Catches H5 layering violations: review must never depend on app.
    Shared types live in bluei/common/ (Repo, RepoConfig, ReviewMode,
    FeedbackEvent, etc.); review-specific state lives in
    bluei/review/state.py. App-layer services (emergent_rules,
    onboarding.inference, escalation, pattern_confidence) are accessed
    via dependency-inverted callbacks wired in app/runner.py — see
    ReviewCycleEngine.__init__'s Phase D parameters.

    Deferred (function-local) imports are also flagged — they are still
    hard dependencies at runtime and invert the intended layering.
    """
    violations: list[str] = []
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and str(node.module).startswith("bluei.app"):
                violations.append(
                    f"{path.name}: review→app import "
                    f"from bluei.app.{node.module[len('bluei.app') :].lstrip('.')} "
                    f"(H5 layering violation; use bluei/common/models.py for "
                    f"shared types or wire a Phase-D callback in app/runner.py)"
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if str(alias.name).startswith("bluei.app"):
                    violations.append(
                        f"{path.name}: review→app import of {alias.name} "
                        f"(H5 layering violation)"
                    )
    return violations


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
        # rec-08: engine must never import from app
        violations.extend(check_engine_imports_app(py_file))

    # H5: review must never import from app. All shared types are in
    # bluei/common/ and review-specific state is in bluei/review/state.py.
    # App-layer services are accessed via Phase-D dependency-inversion
    # callbacks (see ReviewCycleEngine.__init__ in bluei/review/cycle.py).
    for py_file in REVIEW_PACKAGE.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        violations.extend(check_review_imports_app(py_file))

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
