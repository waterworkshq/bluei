"""Argument parsing for the bluei engine CLI.

Extracted from cli.py main() to allow independent testing and reuse.
"""

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from bluei.engine.constants import (
    BASELINE_VALIDATION_CHECKS,
    DEFAULT_BATCH_STATE,
    DEFAULT_CLAUDE_CMD_TEMPLATE,
    DEFAULT_DOCS_INDEX,
    DEFAULT_FINDING_COOLDOWN_SECONDS,
    DEFAULT_FIX_ENGINE,
    DEFAULT_FINDINGS,
    DEFAULT_ISSUES,
    DEFAULT_LESSONS_LOG,
    DEFAULT_LOG,
    DEFAULT_REPO,
    DEFAULT_STATE,
    DEFAULT_STATUS,
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    DEFAULT_WORKTREE_ROOT,
    load_validation_config,
)
from bluei.engine.models import FixEngine


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the bluei engine.

    Returns:
        Parsed argument namespace with all engine configuration.
    """
    p = argparse.ArgumentParser(
        description="SAFE local sandbox QA workflow runner (v2 hardening)"
    )
    p.add_argument("--repo-path", default=str(DEFAULT_REPO))
    p.add_argument("--state-file", default=str(DEFAULT_STATE))
    p.add_argument("--log-file", default=str(DEFAULT_LOG))
    p.add_argument("--findings-file", default=str(DEFAULT_FINDINGS))
    p.add_argument("--issues-file", default=str(DEFAULT_ISSUES))
    p.add_argument("--worktree-root", default=str(DEFAULT_WORKTREE_ROOT))
    p.add_argument("--status-file", default=str(DEFAULT_STATUS))
    p.add_argument("--docs-index-file", default=str(DEFAULT_DOCS_INDEX))
    p.add_argument("--reconcile-only", action="store_true", default=False)
    p.add_argument(
        "--run-phase",
        choices=[
            "issue-cycle",
            "pr-cycle",
            "merge-cycle",
            "refactor-cycle",
            "orchestrated",
            "verify-only",
            "detect-only",
            "e2e",
            "docs-index",
            "clean-prs",
            "smoke-test",
        ],
        default="orchestrated",
    )
    p.add_argument(
        "--smoke-test",
        action="store_true",
        default=False,
        help="Run pre-flight smoke test (repo, git, worktree, linter) and exit",
    )

    # Safety defaults
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    p.add_argument("--live-github-actions", action="store_true", default=False)
    p.add_argument("--max-prs-per-run", type=int, default=2)
    p.add_argument("--allow-main-commit", action="store_true", default=False)
    p.add_argument("--force-push", action="store_true", default=False)

    # Discovery/creation policy
    p.add_argument("--max-issues-per-run", type=int, default=10)
    p.add_argument("--refresh-docs-index", action="store_true", default=False)
    p.add_argument("--issue-confidence-threshold", type=float, default=0.7)
    p.add_argument("--open-issues-cap", type=int, default=20)
    p.add_argument("--open-prs-cap", type=int, default=5)
    p.add_argument("--simulate-open-issues", type=int)
    p.add_argument("--simulate-open-prs", type=int)
    p.add_argument(
        "--finding-cooldown-seconds", type=int, default=DEFAULT_FINDING_COOLDOWN_SECONDS
    )
    p.add_argument(
        "--migrate-context",
        action="store_true",
        default=False,
        help="Reclassify findings with context rules and migrate issue state",
    )
    p.add_argument(
        "--staleness-threshold-seconds",
        type=int,
        default=DEFAULT_STALENESS_THRESHOLD_SECONDS,
    )
    p.add_argument("--auto-merge-sandbox", action="store_true", default=False)
    p.add_argument("--merge-cooldown-minutes", type=int, default=30)
    p.add_argument(
        "--regression-check",
        action="store_true",
        default=False,
        help="Enable regression detection before merging (checks test deletions, export changes, lint regressions)",
    )
    p.add_argument(
        "--auto-rebase-enabled",
        action="store_true",
        default=False,
        help="Enable post-merge rebase sweep across sibling PRs",
    )
    p.add_argument(
        "--rebase-max-prs",
        type=int,
        default=5,
        help="Max sibling PRs to rebase in one sweep",
    )
    p.add_argument(
        "--rebase-stats-file",
        type=Path,
        default=None,
        help="Path to rebase telemetry JSONL (default: state/rebase_stats.jsonl)",
    )
    p.add_argument(
        "--max-queue-items",
        type=int,
        default=None,
        help="Maximum number of refactor queue items to process per run (default: all approved)",
    )
    p.add_argument(
        "--auto-approve",
        action="store_true",
        default=False,
        help="Auto-approve pending_review items before processing (use with refactor-cycle)",
    )
    p.add_argument(
        "--max-fix-attempts-per-issue",
        type=int,
        default=3,
        help="Maximum autofix verification attempts per issue before escalating to human (default: 3)",
    )
    p.add_argument(
        "--fix-engine",
        choices=[FixEngine.DETERMINISTIC.value, FixEngine.CLAUDE.value],
        default=DEFAULT_FIX_ENGINE,
    )
    p.add_argument(
        "--deterministic-only",
        action="store_true",
        default=False,
        help="Only run deterministic cascade stages, skip LLM fallback",
    )
    p.add_argument(
        "--max-duplicate-prs-threshold",
        type=int,
        default=3,
        help="Threshold for max duplicate PR escalation (default: 3)",
    )
    p.add_argument(
        "--no-auto-close-duplicate-prs",
        action="store_true",
        default=False,
        help="Disable auto-closing excess duplicate PRs (detection still runs)",
    )
    p.add_argument("--claude-cmd-template", default=DEFAULT_CLAUDE_CMD_TEMPLATE)

    # Fix scope policy
    p.add_argument("--max-files-changed", type=int, default=5)
    p.add_argument("--max-loc-diff", type=int, default=200)

    # Validation policy
    p.add_argument(
        "--allow-unchanged-baseline-failures",
        dest="allow_unchanged_baseline_failures",
        action="store_true",
        default=True,
    )
    p.add_argument(
        "--no-allow-unchanged-baseline-failures",
        dest="allow_unchanged_baseline_failures",
        action="store_false",
    )

    # Baseline checks (per-repo validation commands, JSON list of lists)
    p.add_argument(
        "--baseline-checks",
        default="[]",
        help='JSON list of baseline check command lists, e.g. \'[["npm","test"],["npm","run","build"]]\'',
    )

    # Review loop scaffolding
    p.add_argument("--pr-author", default="qa-bot")
    p.add_argument("--bot-author", default="qa-bot")
    p.add_argument("--pr-tags", default="")
    p.add_argument("--explicit-tag", default="qa-autofix-ok")
    p.add_argument("--review-feedback", default="")
    p.add_argument(
        "--log-lesson",
        dest="log_lesson",
        default="",
        help="Manual lesson entry to append to LESSONS_LOG.md",
    )
    p.add_argument("--lessons-file", default=str(DEFAULT_LESSONS_LOG))
    # Batch PR engine (Phase 1)
    p.add_argument(
        "--batch-pr-enabled",
        action="store_true",
        default=True,
        help="Enable batch PR grouping for related findings",
    )
    p.add_argument(
        "--no-batch-pr",
        dest="batch_pr_enabled",
        action="store_false",
        help="Disable batch PR grouping",
    )
    p.add_argument(
        "--batch-pr-rules",
        type=Path,
        default=None,
        help="Path to batch_rules.yaml (default: built-in rules)",
    )
    p.add_argument(
        "--batch-state-file",
        default=str(DEFAULT_BATCH_STATE),
        help="Path to batch state JSONL file (default: state/batches.jsonl)",
    )
    p.add_argument(
        "--no-batch-pr-split-on-failure",
        dest="batch_pr_split_on_failure",
        action="store_false",
        default=True,
        help="Do not split batches on fix failures",
    )
    p.add_argument(
        "--batch-dedup-hours",
        type=int,
        default=24,
        help="Max age (hours) for existing batch PRs to be considered duplicates (default: 24)",
    )

    p.add_argument(
        "--pattern-store-path",
        type=Path,
        default=None,
        help="Path to fix_patterns.jsonl for pattern learning/replay (default: disabled)",
    )

    args = p.parse_args()

    # Ensure a basic handler is configured so logger.info(...) prints to stderr
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    return args


def resolve_baseline_checks(args: argparse.Namespace) -> Dict[str, List[str]]:
    """Parse --baseline-checks JSON and resolve to named-dict format.

    Returns:
        Dict mapping 'baseline-N' to command lists.
    """
    _parsed: Dict[str, List[str]] = {}
    try:
        raw: List[List[str]] = json.loads(args.baseline_checks)
        for idx, cmd in enumerate(raw):
            if cmd:
                _parsed[f"baseline-{idx}"] = cmd
    except (json.JSONDecodeError, TypeError):
        _parsed = {}
    return (
        _parsed if _parsed else load_validation_config()["baseline_validation_checks"]
    )


def normalize_run_phase(args: argparse.Namespace) -> None:
    """Normalize legacy phase aliases on args in-place."""
    if args.run_phase == "detect-only":
        args.run_phase = "issue-cycle"
    elif args.run_phase == "e2e":
        args.run_phase = "orchestrated"
