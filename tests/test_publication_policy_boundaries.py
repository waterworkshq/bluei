"""Publication policy boundaries: every mutating git/GitHub boundary must
consult its safety gate UNCONDITIONALLY. The historical ``if safety_config:``
truthy guards failed open — a missing/empty policy skipped the gate entirely.
Now the gate itself decides (deny on missing/empty/invalid), and these tests
pin the caller-side contract: no bypass by policy shape at the boundary.

Dry-run previews perform no mutation and need no policy — they return success
even when the gate would deny the real operation.

All subprocess entry points (git push, gh pr create/merge) are inert mocks.
These tests make NO network calls and run no real git/gh binaries.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.batch_pr_creation import create_batch_pr
from bluei.engine.git_ops import git_push_branch
from bluei.engine.gh import create_or_update_github_pr, merge_pr
from bluei.engine.models import BatchGroup

DENY = (False, "gate-denied")

# Policy shapes that previously bypassed the gate via the truthy guard.
FAIL_OPEN_SHAPES = [None, {}]


def _batch(tmp_path: Path) -> BatchGroup:
    batch = BatchGroup(batch_id="batch-test", rule_pattern="ruff-c408")
    batch.branch = "qa/batch-c408-1"
    batch.worktree_path = tmp_path / "wt"
    return batch


class TestPushBoundary:
    @pytest.mark.parametrize("safety_config", FAIL_OPEN_SHAPES)
    def test_gate_consulted_even_without_policy(self, tmp_path, safety_config):
        log = tmp_path / "log.txt"
        with (
            patch("bluei.engine.git_ops.check_push_allowed", return_value=DENY) as gate,
            patch("bluei.engine.git_ops.run_capture") as push,
        ):
            assert git_push_branch(tmp_path, "qa/fix-1", log, False, safety_config) is False
        gate.assert_called_once_with("qa/fix-1", safety_config)
        push.assert_not_called()

    def test_block_push_policy_blocks_with_real_gate(self, tmp_path):
        log = tmp_path / "log.txt"
        with patch("bluei.engine.git_ops.run_capture") as push:
            assert (
                git_push_branch(
                    tmp_path, "qa/fix-1", log, False, {"block_push": True}
                )
                is False
            )
        push.assert_not_called()

    def test_protected_branch_blocked_with_real_gate(self, tmp_path):
        log = tmp_path / "log.txt"
        with patch("bluei.engine.git_ops.run_capture") as push:
            assert (
                git_push_branch(
                    tmp_path, "main", log, False, {"protected_branches": ["main"]}
                )
                is False
            )
        push.assert_not_called()

    def test_feature_branch_allowed_still_pushes(self, tmp_path):
        log = tmp_path / "log.txt"
        with patch(
            "bluei.engine.git_ops.run_capture", return_value=(0, "")
        ) as push:
            assert (
                git_push_branch(
                    tmp_path, "qa/fix-1", log, False, {"mode": "merge"}
                )
                is True
            )
        push.assert_called_once()


class TestMergeBoundary:
    @pytest.mark.parametrize("safety_config", FAIL_OPEN_SHAPES)
    def test_gate_consulted_even_without_policy(self, tmp_path, safety_config):
        with (
            patch("bluei.engine.gh.pr_ops.check_merge_allowed", return_value=DENY) as gate,
            patch("bluei.engine.gh.run_capture") as gh,
        ):
            ok, reason = merge_pr("o/r", 7, False, tmp_path, safety_config)
        assert ok is False
        assert reason == "blocked-by-safety-mode: gate-denied"
        gate.assert_called_once_with(safety_config)
        gh.assert_not_called()

    def test_pr_mode_blocks_with_real_gate(self, tmp_path):
        with patch("bluei.engine.gh.run_capture") as gh:
            ok, reason = merge_pr(
                "o/r", 7, False, tmp_path, {"mode": "pr"}
            )
        assert ok is False
        assert "pr mode forbids auto-merge" in reason
        gh.assert_not_called()

    def test_merge_mode_allowed_merges(self, tmp_path):
        with patch(
            "bluei.engine.gh.run_capture", return_value=(0, "merged")
        ) as gh:
            ok, reason = merge_pr(
                "o/r", 7, False, tmp_path, {"mode": "merge"}
            )
        assert (ok, reason) == (True, "merged")
        gh.assert_called_once()


class TestPrCreateBoundary:
    def test_gate_consulted_even_without_policy(self, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.touch()
        for safety_config in FAIL_OPEN_SHAPES:
            with (
                patch(
                    "bluei.engine.gh.find_existing_github_pr", return_value=None
                ),
                patch(
                    "bluei.engine.gh.pr_ops.check_pr_creation_allowed",
                    return_value=DENY,
                ) as gate,
                patch("bluei.engine.gh.run_capture") as gh,
            ):
                result = create_or_update_github_pr(
                    "o/r", make_finding(), "qa/fix-1", None, False,
                    log, tmp_path, safety_config=safety_config,
                )
            assert result["error"] == "blocked-by-safety-mode"
            assert result["created"] is False
            gate.assert_called_once()
            gh.assert_not_called()

    def test_observe_mode_blocks_with_real_gate(self, tmp_path, make_finding):
        log = tmp_path / "log.txt"
        log.touch()
        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch("bluei.engine.gh.run_capture") as gh,
        ):
            result = create_or_update_github_pr(
                "o/r", make_finding(), "qa/fix-1", None, False,
                log, tmp_path, safety_config={"mode": "observe"},
            )
        assert result["error"] == "blocked-by-safety-mode"
        gh.assert_not_called()

    def test_existing_open_pr_pure_read_returns_without_mutation(
        self, tmp_path, make_finding
    ):
        """Reusing an existing OPEN PR is a read-only lookup: it may return
        even under a denying policy, and must not create or update anything."""
        log = tmp_path / "log.txt"
        log.touch()
        existing = {"number": 42, "url": "https://github.com/o/r/pull/42", "state": "OPEN"}
        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=existing),
            patch(
                "bluei.engine.gh.pr_ops.check_pr_creation_allowed",
                return_value=DENY,
            ),
            patch("bluei.engine.gh.run_capture") as gh,
        ):
            result = create_or_update_github_pr(
                "o/r", make_finding(), "qa/fix-1", None, False,
                log, tmp_path, safety_config=None,
            )
        assert result == {
            "number": 42,
            "url": "https://github.com/o/r/pull/42",
            "created": False,
        }
        gh.assert_not_called()


class TestBatchPrBoundary:
    @pytest.mark.parametrize("safety_config", FAIL_OPEN_SHAPES)
    def test_gate_consulted_even_without_policy(self, tmp_path, safety_config):
        log = tmp_path / "log.txt"
        with (
            patch(
                "bluei.engine.safety_gates.check_pr_creation_allowed",
                return_value=DENY,
            ) as gate,
            patch("bluei.engine.utils.run_capture") as gh,
        ):
            with pytest.raises(RuntimeError, match="blocked-by-safety-mode"):
                create_batch_pr(_batch(tmp_path), "o/r", log, safety_config)
        gate.assert_called_once()
        gh.assert_not_called()

    def test_issue_only_mode_blocks_with_real_gate(self, tmp_path):
        log = tmp_path / "log.txt"
        with patch("bluei.engine.utils.run_capture") as gh:
            with pytest.raises(RuntimeError, match="issue-only mode forbids"):
                create_batch_pr(
                    _batch(tmp_path), "o/r", log, {"mode": "issue-only"}
                )
        gh.assert_not_called()

    def test_pr_mode_allowed_creates(self, tmp_path):
        log = tmp_path / "log.txt"
        with patch(
            "bluei.engine.utils.run_capture",
            return_value=(0, "https://github.com/o/r/pull/9\n"),
        ) as gh:
            result = create_batch_pr(
                _batch(tmp_path), "o/r", log, {"mode": "pr"}
            )
        assert result["number"] == 9
        gh.assert_called_once()


class TestSuppliedPolicyDenialPrecedence:
    """A supplied restrictive policy denies EVEN IN DRY-RUN — the block is
    reported instead of the simulation (safety_block_takes_precedence_over_dry_run)."""

    def test_push_protected_branch_blocked_in_dry_run(self, tmp_path):
        log = tmp_path / "log.txt"
        with patch("bluei.engine.git_ops.run_capture") as push:
            assert (
                git_push_branch(
                    tmp_path,
                    "main",
                    log,
                    True,
                    {"mode": "pr", "protected_branches": ["main"]},
                )
                is False
            )
        push.assert_not_called()

    def test_merge_pr_mode_blocked_in_dry_run(self, tmp_path):
        with patch("bluei.engine.gh.run_capture") as gh:
            ok, reason = merge_pr("o/r", 42, True, tmp_path, {"mode": "pr"})
        assert ok is False
        assert "blocked-by-safety-mode" in reason
        gh.assert_not_called()

    def test_supplied_permissive_policy_dry_run_still_simulates(self, tmp_path):
        log = tmp_path / "log.txt"
        with (
            patch("bluei.engine.git_ops.run_capture") as push,
        ):
            assert (
                git_push_branch(
                    tmp_path, "qa/fix-1", log, True, {"mode": "merge"}
                )
                is True
            )
        push.assert_not_called()


class TestDryRunNeedsNoPolicy:
    """Dry-run previews mutate nothing — with NO supplied policy they
    simulate (no policy needed for a preview); a supplied policy's denial
    takes precedence (see TestSuppliedPolicyDenialPrecedence)."""

    def test_push_dry_run_unblocked_by_denying_gate(self, tmp_path):
        log = tmp_path / "log.txt"
        with (
            patch("bluei.engine.git_ops.check_push_allowed", return_value=DENY) as gate,
            patch("bluei.engine.git_ops.run_capture") as push,
        ):
            assert (
                git_push_branch(tmp_path, "qa/fix-1", log, True, None) is True
            )
        gate.assert_not_called()
        push.assert_not_called()

    def test_merge_dry_run_unblocked_by_denying_gate(self, tmp_path):
        with (
            patch("bluei.engine.gh.pr_ops.check_merge_allowed", return_value=DENY) as gate,
            patch("bluei.engine.gh.run_capture") as gh,
        ):
            ok, reason = merge_pr("o/r", 7, True, tmp_path, None)
        assert (ok, reason) == (True, "dry-run-merge-simulated")
        gate.assert_not_called()
        gh.assert_not_called()

    def test_pr_create_dry_run_unblocked_by_denying_gate(
        self, tmp_path, make_finding
    ):
        log = tmp_path / "log.txt"
        log.touch()
        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch(
                "bluei.engine.gh.pr_ops.check_pr_creation_allowed",
                return_value=DENY,
            ) as gate,
            patch("bluei.engine.gh.run_capture") as gh,
        ):
            result = create_or_update_github_pr(
                "o/r", make_finding(), "qa/fix-1", None, True,
                log, tmp_path, safety_config=None,
            )
        assert result == {"number": None, "url": "", "created": True}
        gate.assert_not_called()
        gh.assert_not_called()
