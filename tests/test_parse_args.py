"""Tests for bluei.engine.commands.parse_args — argument parsing, baseline resolution, phase normalization."""

import json
import sys
from unittest.mock import patch

import pytest

from bluei.engine.commands.parse_args import (
    normalize_run_phase,
    parse_args,
    resolve_baseline_checks,
)


def _parse(args_list):
    with patch.object(sys, "argv", ["bluei"] + args_list):
        return parse_args()


class TestParseArgsDefaults:
    def test_default_dry_run_is_true(self):
        args = _parse([])
        assert args.dry_run is True

    def test_no_dry_run_flag(self):
        args = _parse(["--no-dry-run"])
        assert args.dry_run is False

    def test_live_github_actions_default_false(self):
        args = _parse([])
        assert args.live_github_actions is False

    def test_run_phase_default_orchestrated(self):
        args = _parse([])
        assert args.run_phase == "orchestrated"

    def test_custom_run_phase(self):
        args = _parse(["--run-phase", "pr-cycle"])
        assert args.run_phase == "pr-cycle"

    def test_fix_engine_default(self):
        args = _parse([])
        assert args.fix_engine in ("deterministic", "claude")

    def test_max_issues_per_run_default(self):
        args = _parse([])
        assert args.max_issues_per_run == 10

    def test_custom_int_args(self):
        args = _parse(
            [
                "--max-issues-per-run",
                "5",
                "--open-issues-cap",
                "30",
                "--max-fix-attempts-per-issue",
                "7",
            ]
        )
        assert args.max_issues_per_run == 5
        assert args.open_issues_cap == 30
        assert args.max_fix_attempts_per_issue == 7

    def test_reconcile_only_flag(self):
        args = _parse(["--reconcile-only"])
        assert args.reconcile_only is True

    def test_batch_pr_enabled_default(self):
        args = _parse([])
        assert args.batch_pr_enabled is True

    def test_no_batch_pr_flag(self):
        args = _parse(["--no-batch-pr"])
        assert args.batch_pr_enabled is False


class TestResolveBaselineChecks:
    def test_valid_json_commands(self):
        args = _parse(["--baseline-checks", '[["npm","test"],["npm","run","build"]]'])
        result = resolve_baseline_checks(args)
        assert result == {
            "baseline-0": ["npm", "test"],
            "baseline-1": ["npm", "run", "build"],
        }

    def test_empty_commands_filtered(self):
        args = _parse(["--baseline-checks", '[["npm","test"],[],["make"]]'])
        result = resolve_baseline_checks(args)
        assert "baseline-1" not in result
        assert len(result) == 2

    def test_invalid_json_returns_defaults(self):
        args = _parse(["--baseline-checks", "not-json"])
        result = resolve_baseline_checks(args)
        assert isinstance(result, dict)
        assert len(result) > 0


class TestNormalizeRunPhase:
    def test_detect_only_becomes_issue_cycle(self):
        args = _parse(["--run-phase", "detect-only"])
        normalize_run_phase(args)
        assert args.run_phase == "issue-cycle"

    def test_e2e_becomes_orchestrated(self):
        args = _parse(["--run-phase", "e2e"])
        normalize_run_phase(args)
        assert args.run_phase == "orchestrated"

    def test_pr_cycle_unchanged(self):
        args = _parse(["--run-phase", "pr-cycle"])
        normalize_run_phase(args)
        assert args.run_phase == "pr-cycle"
