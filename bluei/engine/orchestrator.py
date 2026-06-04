"""Cycle orchestration: command builders and finding discovery.

Builds CLI command strings for each run phase (active, issue, PR, merge,
refactor, etc.) and dispatches the main `discover_findings` scan which
combines string-based detectors, AST analysis, linters, and language plugins
into a unified Finding list."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from bluei.engine.constants import (
    CLAUDE_REQUIRED_RULES,
    DEFAULT_DOCS_INDEX,
    DEFAULT_LOG,
    DETECTOR_CATALOG,
    MAX_LINES_REFACTOR_LIMIT,
    MAX_LINES_REFACTOR_TARGET,
    RULE_TARGET_CHECKS,
    RUNNER_PATH,
)
from bluei.engine.git_utils import _git_last_commit_for_path, load_docs_index
from bluei.engine.linters import (
    discover_python_linter_findings,
    discover_test_coverage_findings,
    discover_typescript_type_findings,
    discover_xo_linter_findings,
)
from bluei.engine.models import Finding, now_iso, parse_iso, stable_finding_id
from bluei.engine.plugin_loader import run_plugin_discovery
from bluei.engine.reforge import (
    RefactorClass,
    RefactorWork,
    can_auto_refactor,
    classify_finding,
)
from bluei.engine.refactor_queue import enqueue_refactor_work
from bluei.engine.state import _append_text, save_refactor_work
from bluei.engine.utils import command_list_to_shell

_logger = logging.getLogger(__name__)

# Directories to skip during AST file walks (dependency/cache/artifact dirs).
_AST_SKIP_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    "site-packages",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

_TS_SKIP_DIRS = _AST_SKIP_DIRS | {"dist", "build", ".next", "coverage", "vendor"}


# ---------------------------------------------------------------------------
# Parameterised AST scan pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LanguageScanConfig:
    """Configuration for a single language's AST scan pipeline."""

    name: str
    extensions: Set[str]
    get_matcher: Callable[[], Any]
    skip_dirs: Set[str] = field(default_factory=lambda: _AST_SKIP_DIRS)
    language: Optional[str] = None
    ext_to_language: Optional[Dict[str, str]] = None
    needs_tree_sitter: bool = False


def _python_getter():
    from bluei.engine.ast_engine import get_python_matcher

    return get_python_matcher()


def _ts_getter():
    from bluei.engine.ast_engine import get_ts_matcher

    return get_ts_matcher()


def _go_getter():
    from bluei.engine.ast_engine import get_go_matcher

    return get_go_matcher()


def _rust_getter():
    from bluei.engine.ast_engine import get_rust_matcher

    return get_rust_matcher()


LANGUAGE_SCAN_CONFIGS: Dict[str, LanguageScanConfig] = {
    "python": LanguageScanConfig(
        name="python",
        extensions={".py"},
        get_matcher=_python_getter,
        skip_dirs=_AST_SKIP_DIRS,
        language="python",
        needs_tree_sitter=False,
    ),
    "typescript": LanguageScanConfig(
        name="typescript",
        extensions={".ts", ".tsx", ".js", ".jsx"},
        get_matcher=_ts_getter,
        skip_dirs=_TS_SKIP_DIRS,
        ext_to_language={
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
        },
        needs_tree_sitter=True,
    ),
    "go": LanguageScanConfig(
        name="go",
        extensions={".go"},
        get_matcher=_go_getter,
        skip_dirs=_TS_SKIP_DIRS,
        language="go",
        needs_tree_sitter=True,
    ),
    "rust": LanguageScanConfig(
        name="rust",
        extensions={".rs"},
        get_matcher=_rust_getter,
        skip_dirs=_TS_SKIP_DIRS,
        language="rust",
        needs_tree_sitter=True,
    ),
}


def ast_scan_pipeline(
    repo_path: Path,
    log_file: Path,
    rule_meta: dict,
    config: LanguageScanConfig,
) -> List[Finding]:
    """Run AST pattern matching for a single language.

    Args:
        repo_path: Absolute path to the target repository.
        log_file: Path to the run log (unused, kept for signature parity).
        rule_meta: Dict mapping rule IDs to their catalog entries.
        config: Language scan configuration.

    Returns:
        List of Finding objects for every matched AST pattern.
    """
    if config.needs_tree_sitter:
        from bluei.engine.ast_engine.ts_parser import TreeSitterAdapter

        if not TreeSitterAdapter.is_available():
            return []

    matcher = config.get_matcher()
    findings: List[Finding] = []

    for ext in config.extensions:
        for f in repo_path.rglob(f"*{ext}"):
            skip = False
            for part in f.relative_to(repo_path).parts:
                if part in config.skip_dirs:
                    skip = True
                    break
            if skip:
                continue

            try:
                source = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            if not source.strip():
                continue

            if config.ext_to_language:
                language = config.ext_to_language.get(ext, config.name)
            else:
                language = config.language or config.name

            rel_path = str(f.relative_to(repo_path))
            matches = matcher.find_matches(source, rel_path, language)

            for m in matches:
                rule_entry = rule_meta.get(m.pattern.id)
                if rule_entry is None:
                    # AST engine uses the full rule id; catalog stores the shorter form.
                    if m.pattern.id == "hardcoded-tmp-path-string":
                        rule_entry = rule_meta.get("hardcoded-tmp-path")
                if rule_entry is None:
                    continue

                findings.append(
                    Finding(
                        finding_id=stable_finding_id(
                            str(repo_path),
                            rel_path,
                            m.line,
                            m.pattern.id,
                            m.source_text,
                        ),
                        repo=str(repo_path),
                        path=rel_path,
                        line=m.line,
                        rule=m.pattern.id,
                        snippet=m.source_text,
                        confidence=rule_entry.get("confidence", m.pattern.confidence),
                        quick_win=rule_entry.get("autofix", False),
                        safe_to_autofix=rule_entry.get("autofix", False),
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Backward-compatible wrappers (used as monkeypatch targets in tests)
# ---------------------------------------------------------------------------


def _ast_scan_python_files(
    repo_path: Path, log_file: Path, rule_meta: dict
) -> List[Finding]:
    """Walk *.py files under repo_path and run AST pattern matching."""
    return ast_scan_pipeline(
        repo_path, log_file, rule_meta, LANGUAGE_SCAN_CONFIGS["python"]
    )


def _ast_scan_ts_js_files(
    repo_path: Path, log_file: Path, rule_meta: dict
) -> List[Finding]:
    """Walk .ts/.tsx/.js/.jsx files and run tree-sitter AST matching."""
    return ast_scan_pipeline(
        repo_path, log_file, rule_meta, LANGUAGE_SCAN_CONFIGS["typescript"]
    )


def _ast_scan_go_files(
    repo_path: Path, log_file: Path, rule_meta: dict
) -> List[Finding]:
    """Walk .go files and run tree-sitter AST matching."""
    return ast_scan_pipeline(
        repo_path, log_file, rule_meta, LANGUAGE_SCAN_CONFIGS["go"]
    )


def _ast_scan_rust_files(
    repo_path: Path, log_file: Path, rule_meta: dict
) -> List[Finding]:
    """Walk .rs files and run tree-sitter AST matching."""
    return ast_scan_pipeline(
        repo_path, log_file, rule_meta, LANGUAGE_SCAN_CONFIGS["rust"]
    )


# ---------------------------------------------------------------------------
# Command builders — re-exported from command_builder.py
# ---------------------------------------------------------------------------

from bluei.engine.command_builder import (  # noqa: E402, F401
    build_active_cycle_command,
    build_docs_index_refresh_command,
    build_issue_cycle_command,
    build_merge_cycle_command,
    build_orchestrated_cycle_command,
    build_pr_cycle_command,
    build_reconcile_only_command,
    build_refactor_cycle_command,
    build_verification_only_command,
)


def discover_findings(
    repo_path: Path,
    log_file: Path = DEFAULT_LOG,
    docs_index_file: Path = DEFAULT_DOCS_INDEX,
) -> List[Finding]:
    """
    Main public entry for finding discovery.

    Args:
        repo_path: Absolute path to the target repository.
        log_file: Path to the run log for progress messages.
        docs_index_file: Path to the docs-index YAML for gap/drift detectors.

    Returns:
        Deduplicated list of Finding objects across all detection sources.
    """
    # Skip internal discovery for ky repos
    if "--skip-internal-discovery" in os.environ:
        _append_text(
            log_file,
            "discover_findings: skipping internal discovery for ky repo - using external discovery",
        )
        return []

    findings: List[Finding] = []

    def _read_lines(relative_path: str) -> List[str]:
        target = repo_path / relative_path
        if not target.exists():
            return []
        return target.read_text(encoding="utf-8").splitlines()

    def _add_finding(
        relative_path: str,
        line_number: int,
        rule: str,
        snippet: str,
        confidence: float,
        quick_win: bool = False,
        safe_to_autofix: bool = False,
    ) -> None:
        findings.append(
            Finding(
                finding_id=stable_finding_id(
                    str(repo_path), relative_path, line_number, rule, snippet
                ),
                repo=str(repo_path),
                path=relative_path,
                line=line_number,
                rule=rule,
                snippet=snippet,
                confidence=confidence,
                quick_win=quick_win,
                safe_to_autofix=safe_to_autofix,
            )
        )

    rule_meta = {entry["rule"]: entry for entry in DETECTOR_CATALOG}

    orders_lines = _read_lines("src/qa_sandbox/orders.py")
    for idx, line in enumerate(orders_lines, start=1):
        if "int(order.subtotal * order.tax_rate)" in line:
            _add_finding(
                "src/qa_sandbox/orders.py",
                idx,
                "orders-tax-truncation",
                line.strip(),
                rule_meta["orders-tax-truncation"]["confidence"],
            )

    inventory_lines = _read_lines("src/qa_sandbox/inventory.py")
    if inventory_lines and not any("quantity <= 0" in line for line in inventory_lines):
        for idx, line in enumerate(inventory_lines, start=1):
            if "stock[sku] < quantity" in line:
                _add_finding(
                    "src/qa_sandbox/inventory.py",
                    idx,
                    "inventory-invalid-quantity",
                    "missing quantity <= 0 guard",
                    rule_meta["inventory-invalid-quantity"]["confidence"],
                    quick_win=True,
                    safe_to_autofix=True,
                )
                break

    # Debt/TODO detectors
    todo_targets = [
        "src/qa_sandbox/catalog.py",
        "src/qa_sandbox/orders.py",
        "src/qa_sandbox/analytics.py",
        "scripts/report_health.py",
    ]
    for rel_path in todo_targets:
        for idx, line in enumerate(_read_lines(rel_path), start=1):
            stripped = line.strip()
            if stripped.startswith("# TODO:") or stripped.startswith("# FIXME:"):
                _add_finding(
                    rel_path,
                    idx,
                    "debt-todo-marker",
                    stripped,
                    rule_meta["debt-todo-marker"]["confidence"],
                )

    # Trailing whitespace detector for Python source files
    py_files = [
        "src/qa_sandbox/catalog.py",
        "src/qa_sandbox/orders.py",
        "src/qa_sandbox/notifications.py",
        "src/qa_sandbox/inventory.py",
        "src/qa_sandbox/analytics.py",
        "scripts/report_health.py",
        "price.py",
    ]
    for rel_path in py_files:
        for idx, line in enumerate(_read_lines(rel_path), start=1):
            # Check for trailing whitespace (space or tab at end of non-empty line)
            if line and line != line.rstrip():
                _add_finding(
                    rel_path,
                    idx,
                    "trailing-whitespace",
                    f"line ends with whitespace: '{line[-10:]}'",
                    rule_meta["trailing-whitespace"]["confidence"],
                    quick_win=True,
                    safe_to_autofix=True,
                )

    # Docs detectors
    for rel_path in ["docs/ARCHITECTURE.md", "docs/TROUBLESHOOTING.md"]:
        for idx, line in enumerate(_read_lines(rel_path), start=1):
            if "legacy_pricer.py" in line:
                _add_finding(
                    rel_path,
                    idx,
                    "docs-legacy-reference",
                    line.strip(),
                    rule_meta["docs-legacy-reference"]["confidence"],
                    quick_win=True,
                    safe_to_autofix=True,
                )

    operations_lines = _read_lines("docs/OPERATIONS.md")
    operations_text = "\n".join(operations_lines)
    has_rollback_section = (
        "## Rollback" in operations_text or "## rollback" in operations_text.lower()
    )
    has_revert_instruction = (
        "git revert" in operations_text.lower()
        or "revert the" in operations_text.lower()
    )
    if operations_lines and not (has_rollback_section and has_revert_instruction):
        _add_finding(
            "docs/OPERATIONS.md",
            1,
            "docs-missing-rollback",
            "missing rollback runbook section with git revert instructions",
            rule_meta["docs-missing-rollback"]["confidence"],
            quick_win=True,
            safe_to_autofix=True,
        )

    readme_lines = _read_lines("README.md")
    readme_text = "\n".join(readme_lines)
    if (
        "pytest -q" in readme_text
        and "pip install pytest" not in readme_text
        and "uv pip install pytest" not in readme_text
    ):
        _add_finding(
            "README.md",
            1,
            "docs-quickstart-gap",
            "pytest command present without setup/install note",
            rule_meta["docs-quickstart-gap"]["confidence"],
            quick_win=True,
            safe_to_autofix=True,
        )

    # Docs index-backed gap/drift detectors
    docs_index_entries = load_docs_index(docs_index_file)
    for entry in docs_index_entries:
        code_path = str(entry.get("code_path") or "").strip()
        if not code_path:
            continue
        target = repo_path / code_path
        if not target.exists() or not target.is_file():
            continue

        coverage_status = str(entry.get("coverage_status") or "").strip().lower()
        if coverage_status == "uncovered":
            _add_finding(
                code_path,
                1,
                "doc-gap-uncovered-module",
                "missing inline and external docs coverage (from docs index)",
                rule_meta["doc-gap-uncovered-module"]["confidence"],
            )

        # Drift: flag if the code file's SHA or mtime diverges from the index snapshot.
        has_external = bool(entry.get("has_external_doc_ref", False))
        current_sha = _git_last_commit_for_path(repo_path, code_path)
        indexed_sha = str(entry.get("last_seen_sha") or "").strip()
        index_updated = parse_iso(str(entry.get("last_updated") or ""))
        try:
            file_mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
        except Exception:
            file_mtime = None
        changed_since_index = bool(
            index_updated and file_mtime and file_mtime > index_updated
        )
        sha_changed = bool(indexed_sha and current_sha and indexed_sha != current_sha)

        if has_external and (sha_changed or changed_since_index):
            _add_finding(
                code_path,
                1,
                "doc-drift-stale-reference",
                "external docs may be stale after code changes (from docs index)",
                rule_meta["doc-drift-stale-reference"]["confidence"],
            )

    # Test-gap detectors
    notifications_test = repo_path / "tests" / "test_notifications.py"
    if not notifications_test.exists():
        _add_finding(
            "tests/test_notifications.py",
            1,
            "test-gap-missing-file",
            "missing notification tests for invalid input and trimming behavior",
            rule_meta["test-gap-missing-file"]["confidence"],
            quick_win=True,
            safe_to_autofix=True,
        )

    orders_test_lines = _read_lines("tests/test_orders.py")
    if orders_test_lines and not any(
        "invalid" in line.lower() and "coupon" in line.lower()
        for line in orders_test_lines
    ):
        _add_finding(
            "tests/test_orders.py",
            1,
            "test-gap-missing-case",
            "missing invalid coupon behavior test",
            rule_meta["test-gap-missing-case"]["confidence"],
            quick_win=True,
            safe_to_autofix=True,
        )

    inventory_test_lines = _read_lines("tests/test_inventory.py")
    if inventory_test_lines and not any(
        "negative" in line.lower() for line in inventory_test_lines
    ):
        _add_finding(
            "tests/test_inventory.py",
            1,
            "test-gap-missing-case",
            "missing negative quantity test for reserve_stock",
            rule_meta["test-gap-missing-case"]["confidence"],
            quick_win=True,
            safe_to_autofix=True,
        )

    # AST-based detection (supplements string-based detection); dedup by (path, line, rule).
    existing_keys = {(f.path, f.line, f.rule) for f in findings}
    # Call wrappers individually so tests can monkeypatch them.
    ast_scan_calls = [
        ("python", _ast_scan_python_files),
        ("typescript", _ast_scan_ts_js_files),
        ("go", _ast_scan_go_files),
        ("rust", _ast_scan_rust_files),
    ]
    for lang_name, scan_fn in ast_scan_calls:
        lang_findings = scan_fn(repo_path, log_file, rule_meta)
        if lang_findings:
            added = 0
            for af in lang_findings:
                if (af.path, af.line, af.rule) not in existing_keys:
                    findings.append(af)
                    existing_keys.add((af.path, af.line, af.rule))
                    added += 1
            if added:
                _append_text(
                    log_file,
                    f"ast-{lang_name}-discovery: added {added} AST findings (deduped from {len(lang_findings)} total)",
                )

    # Also run type safety discovery for TypeScript repos
    type_findings = discover_typescript_type_findings(repo_path, log_file)
    if type_findings:
        findings.extend(type_findings)
        _append_text(
            log_file, f"type-discovery: added {len(type_findings)} type safety findings"
        )

    # Also run test coverage discovery
    coverage_findings = discover_test_coverage_findings(repo_path, log_file)
    if coverage_findings:
        findings.extend(coverage_findings)
        _append_text(
            log_file,
            f"coverage-discovery: added {len(coverage_findings)} test coverage findings",
        )

    # Run xo linter discovery for TypeScript/JavaScript repos
    xo_findings = discover_xo_linter_findings(repo_path, log_file)
    if xo_findings:
        findings.extend(xo_findings)
        _append_text(
            log_file, f"xo-discovery: added {len(xo_findings)} xo linter findings"
        )

    # Run Python linter discovery for Python repos
    python_findings = discover_python_linter_findings(repo_path, log_file)
    if python_findings:
        findings.extend(python_findings)
        _append_text(
            log_file, f"python-discovery: added {len(python_findings)} ruff findings"
        )

    plugin_findings = run_plugin_discovery(repo_path)
    if plugin_findings:
        findings.extend(plugin_findings)
        _append_text(
            log_file,
            f"plugin-discovery: added {len(plugin_findings)} language pack findings",
        )

    return findings


def choose_safe_autofix_items(
    findings: List[Finding], confidence_threshold: float
) -> List[Finding]:
    return [
        f
        for f in findings
        if f.safe_to_autofix and f.confidence >= confidence_threshold
    ]


def route_findings_with_intent(
    findings: List[Finding],
    confidence_threshold: float,
    findings_file: Optional[Path] = None,
    worktree_path: Optional[Path] = None,
    log_file: Optional[Path] = None,
    detected_frameworks: Optional[List[str]] = None,
) -> Dict[str, List[Any]]:
    """Route findings into intentional execution lanes.

    Buckets:
    - autofix_safe: deterministic low-risk fixes
    - refactor_queue: structural refactor findings, with queue metadata when queued
    - human_review: non-autofix findings needing manual or later LLM handling
    - skipped: below confidence threshold
    """
    routed: Dict[str, List[Any]] = {
        "autofix_safe": [],
        "refactor_queue": [],
        "human_review": [],
        "skipped": [],
    }

    for finding in findings:
        if finding.confidence < confidence_threshold:
            routed["skipped"].append(finding)
            continue

        rc = classify_finding(finding, detected_frameworks)
        if rc == RefactorClass.SIMPLE_FIX and finding.safe_to_autofix:
            routed["autofix_safe"].append(finding)
            continue

        if rc == RefactorClass.REFACTOR_CLASS:
            refactor_work = RefactorWork(finding_id=finding.finding_id)
            finding.refactor_phase = refactor_work.phase.value
            queued_work_id: Optional[str] = None
            route_reason = "planning"

            if worktree_path is not None:
                allowed, reason = can_auto_refactor(finding, worktree_path)
                if not allowed:
                    refactor_work.mark_aborted(reason)
                    finding.refactor_phase = refactor_work.phase.value
                    route_reason = reason
                    entry = enqueue_refactor_work(finding, refactor_work)
                    queued_work_id = entry.work_id

            if findings_file is not None:
                save_refactor_work(finding.finding_id, findings_file, refactor_work)

            if log_file is not None:
                _append_text(
                    log_file,
                    "route-findings: "
                    f"finding_id={finding.finding_id} rule={finding.rule} "
                    f"class={rc.value} phase={refactor_work.phase.value} "
                    f"queued_work_id={queued_work_id or ''} reason={route_reason}",
                )

            routed["refactor_queue"].append(
                {
                    "finding": finding,
                    "refactor_work": refactor_work,
                    "queued_work_id": queued_work_id,
                    "reason": route_reason,
                }
            )
            continue

        routed["human_review"].append(finding)

    return routed


# ---------------------------------------------------------------------------
# Issue lifecycle — re-exported from issue_lifecycle.py
# (Kept here for backward compatibility with monkeypatch targets in tests.)
# ---------------------------------------------------------------------------

from bluei.engine.issue_lifecycle import (  # noqa: E402, F401
    check_consecutive_fix_failures,
    check_finding_escalation_before_fix,
    count_failed_fix_attempts,
    create_issues_for_findings,
    ensure_issue_for_finding,
    find_issue_for_finding,
    set_issue_status,
)


def append_issue_history(
    issue: Dict[str, Any], event: str, detail: Optional[str] = None
) -> None:
    """Re-export from issue_lifecycle — kept for monkeypatch compat."""
    from bluei.engine.issue_lifecycle import append_issue_history as _impl

    _impl(issue, event, detail)
