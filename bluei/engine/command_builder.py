"""Command builders: construct CLI argument strings for each cycle phase.

Moved from orchestrator.py to reduce its surface area."""

from __future__ import annotations

import argparse
from typing import List

from bluei.engine.constants import RUNNER_PATH
from bluei.engine.utils import command_list_to_shell


def _build_base_cycle_command(args: argparse.Namespace) -> List[str]:
    """Assemble the shared CLI argument list used by all cycle command builders.

    Args:
        args: Parsed CLI namespace with repo/state/log/worktree paths and
            throttling knobs.

    Returns:
        List of command-line tokens ready for ``command_list_to_shell``.
    """
    cmd = [
        "python3",
        str(RUNNER_PATH),
        "--repo-path",
        str(args.repo_path),
        "--state-file",
        str(args.state_file),
        "--log-file",
        str(args.log_file),
        "--findings-file",
        str(args.findings_file),
        "--issues-file",
        str(args.issues_file),
        "--worktree-root",
        str(args.worktree_root),
        "--open-issues-cap",
        str(args.open_issues_cap),
        "--open-prs-cap",
        str(args.open_prs_cap),
        "--issue-confidence-threshold",
        str(args.issue_confidence_threshold),
        "--max-files-changed",
        str(args.max_files_changed),
        "--max-loc-diff",
        str(args.max_loc_diff),
        "--max-prs-per-run",
        str(args.max_prs_per_run),
        "--max-issues-per-run",
        str(args.max_issues_per_run),
        "--finding-cooldown-seconds",
        str(args.finding_cooldown_seconds),
        "--merge-cooldown-minutes",
        str(args.merge_cooldown_minutes),
        "--max-fix-attempts-per-issue",
        str(args.max_fix_attempts_per_issue),
        "--docs-index-file",
        str(args.docs_index_file),
        "--fix-engine",
        str(args.fix_engine),
        "--claude-cmd-template",
        str(args.claude_cmd_template),
    ]
    if getattr(args, "refresh_docs_index", False):
        cmd.append("--refresh-docs-index")
    if getattr(args, "live_github_actions", False):
        cmd.append("--live-github-actions")
    if getattr(args, "auto_merge_sandbox", False):
        cmd.append("--auto-merge-sandbox")
    return cmd


def build_active_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + [
        "--run-phase",
        str(args.run_phase),
        "--no-dry-run",
    ]
    return command_list_to_shell(cmd)


def build_issue_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + [
        "--run-phase",
        "issue-cycle",
        "--no-dry-run",
    ]
    return command_list_to_shell(cmd)


def build_pr_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + ["--run-phase", "pr-cycle", "--no-dry-run"]
    return command_list_to_shell(cmd)


def build_merge_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + [
        "--run-phase",
        "merge-cycle",
        "--no-dry-run",
        "--auto-merge-sandbox",
    ]
    return command_list_to_shell(cmd)


def build_orchestrated_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + [
        "--run-phase",
        "orchestrated",
        "--no-dry-run",
        "--auto-merge-sandbox",
    ]
    return command_list_to_shell(cmd)


def build_refactor_cycle_command(args: argparse.Namespace) -> str:
    cmd = _build_base_cycle_command(args) + [
        "--run-phase",
        "refactor-cycle",
        "--no-dry-run",
    ]
    if getattr(args, "max_queue_items", None) is not None:
        cmd.extend(["--max-queue-items", str(args.max_queue_items)])
    if getattr(args, "auto_approve", False):
        cmd.append("--auto-approve")
    return command_list_to_shell(cmd)


def build_reconcile_only_command(args: argparse.Namespace) -> str:
    cmd = [
        "python3",
        str(RUNNER_PATH),
        "--reconcile-only",
        "--repo-path",
        str(args.repo_path),
        "--state-file",
        str(args.state_file),
        "--log-file",
        str(args.log_file),
        "--findings-file",
        str(args.findings_file),
        "--issues-file",
        str(args.issues_file),
        "--worktree-root",
        str(args.worktree_root),
    ]
    if getattr(args, "live_github_actions", False):
        cmd.append("--live-github-actions")
    return command_list_to_shell(cmd)


def build_docs_index_refresh_command(args: argparse.Namespace) -> str:
    cmd = [
        "python3",
        str(RUNNER_PATH),
        "--run-phase",
        "docs-index",
        "--repo-path",
        str(args.repo_path),
        "--log-file",
        str(args.log_file),
        "--docs-index-file",
        str(args.docs_index_file),
        "--refresh-docs-index",
    ]
    return command_list_to_shell(cmd)


def build_verification_only_command(args: argparse.Namespace) -> str:
    cmd = [
        "python3",
        str(RUNNER_PATH),
        "--run-phase",
        "verify-only",
        "--repo-path",
        str(args.repo_path),
        "--state-file",
        str(args.state_file),
        "--log-file",
        str(args.log_file),
        "--findings-file",
        str(args.findings_file),
        "--issues-file",
        str(args.issues_file),
        "--worktree-root",
        str(args.worktree_root),
    ]
    if getattr(args, "live_github_actions", False):
        cmd.append("--live-github-actions")
    return command_list_to_shell(cmd)
