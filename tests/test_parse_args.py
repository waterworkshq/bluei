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
from bluei.engine.constants import clear_validation_config_cache
from bluei.engine.validation import RC_UNAVAILABLE, run_named_checks


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
        assert args.batch_pr_enabled is False

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

    def test_malformed_json_returns_invalid_sentinel(self):
        # An operator typo is preserved as unavailable evidence; no silent
        # fallback to a different workspace configuration.
        args = _parse(["--baseline-checks", "not-json"])
        assert resolve_baseline_checks(args) == {"baseline-invalid": []}

    def test_non_list_json_returns_invalid_sentinel(self):
        args = _parse(["--baseline-checks", '{"baseline-0": ["make"]}'])
        assert resolve_baseline_checks(args) == {"baseline-invalid": []}

    def test_malformed_json_does_not_consult_workspace_config(self):
        with patch(
            "bluei.engine.commands.parse_args.load_validation_config",
            return_value={
                "baseline_validation_checks": {"cfg-0": ["make", "check"]}
            },
        ) as mock_cfg:
            args = _parse(["--baseline-checks", "not-json"])
            assert resolve_baseline_checks(args) == {"baseline-invalid": []}
            mock_cfg.assert_not_called()

    def test_explicit_empty_list_does_not_consult_workspace_config(self):
        with patch("bluei.engine.commands.parse_args.load_validation_config") as workspace:
            assert resolve_baseline_checks(_parse(["--baseline-checks", "[]"])) == {}
            workspace.assert_not_called()

    def test_omitted_checks_use_workspace_config(self):
        with patch(
            "bluei.engine.commands.parse_args.load_validation_config",
            return_value={
                "baseline_validation_checks": {"cfg-0": ["make", "check"]}
            },
        ):
            assert resolve_baseline_checks(_parse([])) == {"cfg-0": ["make", "check"]}

    def test_mixed_entries_all_retained_and_unqualified_when_run(self, tmp_path):
        # Every entry is preserved — including None/[] — so the trust
        # boundary (not the parser) rejects them as unavailable evidence.
        args = _parse(
            ["--baseline-checks", '[["true"], null, []]'],
        )
        resolved = resolve_baseline_checks(args)
        assert resolved == {"baseline-0": ["true"], "baseline-1": None, "baseline-2": []}
        log = tmp_path / "resolve-run.log"
        results = run_named_checks(tmp_path, resolved, log, "baseline")
        assert results["baseline-0"]["available"] is True
        assert results["baseline-0"]["rc"] == 0
        for bad in ("baseline-1", "baseline-2"):
            assert results[bad]["available"] is False
            assert results[bad]["unavailable_reason"] == "invalid-argv"
            assert results[bad]["rc"] == RC_UNAVAILABLE


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
