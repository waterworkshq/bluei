"""Tests for the runtime safety gates (F1: protected-branch enforcement).

Covers:

- ``is_protected_branch`` — branch matching incl. case/whitespace
- ``check_push_allowed`` — protected-branch push gate
- ``check_pr_creation_allowed`` — safety-mode gate for PR creation
- ``check_merge_allowed`` — safety-mode gate for merges
- ``resolve_base_branch`` — base-branch resolution precedence
- Integration: ``create_or_update_github_pr`` blocked in OBSERVE mode
- Integration: ``git_push_branch`` blocked for protected branch
- Integration: ``merge_pr`` blocked in PR mode
"""

from pathlib import Path
from unittest.mock import patch

from bluei.engine.safety_gates import (
    DEFAULT_BASE_BRANCH,
    check_merge_allowed,
    check_pr_creation_allowed,
    check_push_allowed,
    is_protected_branch,
    resolve_base_branch,
)


# ---------------------------------------------------------------------------
# is_protected_branch
# ---------------------------------------------------------------------------


class TestIsProtectedBranch:
    def test_main_protected_by_default(self) -> None:
        assert is_protected_branch("main", ["main", "master"]) is True

    def test_master_protected_by_default(self) -> None:
        assert is_protected_branch("master", ["main", "master"]) is True

    def test_feature_branch_not_protected(self) -> None:
        assert is_protected_branch("qa/fix-typo", ["main", "master"]) is False

    def test_custom_protected_branch(self) -> None:
        assert is_protected_branch("develop", ["main", "develop"]) is True

    def test_case_insensitive(self) -> None:
        assert is_protected_branch("Main", ["main"]) is True
        assert is_protected_branch("MAIN", ["main"]) is True

    def test_whitespace_trimmed(self) -> None:
        assert is_protected_branch("  main  ", ["main"]) is True
        assert is_protected_branch("main", ["  main  "]) is True

    def test_empty_branch_never_protected(self) -> None:
        assert is_protected_branch("", ["main"]) is False

    def test_empty_list_never_protected(self) -> None:
        assert is_protected_branch("main", []) is False

    def test_none_inputs_safe(self) -> None:
        assert is_protected_branch("main", []) is False
        assert is_protected_branch("", ["main"]) is False


# ---------------------------------------------------------------------------
# check_push_allowed
# ---------------------------------------------------------------------------


class TestCheckPushAllowed:
    def test_feature_branch_allowed(self) -> None:
        safety = {"mode": "pr", "protected_branches": ["main", "master"]}
        allowed, reason = check_push_allowed("qa/fix-typo-123", safety)
        assert allowed is True
        assert reason == "ok"

    def test_main_blocked(self) -> None:
        safety = {"mode": "pr", "protected_branches": ["main", "master"]}
        allowed, reason = check_push_allowed("main", safety)
        assert allowed is False
        assert "protected" in reason
        assert "main" in reason

    def test_master_blocked(self) -> None:
        safety = {"mode": "merge", "protected_branches": ["main", "master"]}
        allowed, reason = check_push_allowed("master", safety)
        assert allowed is False

    def test_custom_protected_blocked(self) -> None:
        safety = {"mode": "merge", "protected_branches": ["main", "release"]}
        allowed, _ = check_push_allowed("release", safety)
        assert allowed is False

    def test_no_safety_config_allows(self) -> None:
        allowed, reason = check_push_allowed("main", None)
        assert allowed is True
        assert reason == "no-safety-config"

    def test_empty_safety_config_allows(self) -> None:
        allowed, _ = check_push_allowed("main", {})
        assert allowed is True

    def test_missing_protected_branches_defaults_to_main_master(self) -> None:
        # Even without protected_branches in config, defaults block main/master
        safety = {"mode": "pr"}
        allowed_main, _ = check_push_allowed("main", safety)
        allowed_master, _ = check_push_allowed("master", safety)
        assert allowed_main is False
        assert allowed_master is False

    def test_case_insensitive_blocking(self) -> None:
        safety = {"mode": "pr", "protected_branches": ["main"]}
        assert check_push_allowed("Main", safety)[0] is False


# ---------------------------------------------------------------------------
# check_pr_creation_allowed
# ---------------------------------------------------------------------------


class TestCheckPrCreationAllowed:
    def test_observe_mode_blocked(self) -> None:
        safety = {"mode": "observe", "protected_branches": ["main"]}
        allowed, reason = check_pr_creation_allowed("main", safety)
        assert allowed is False
        assert "observe" in reason

    def test_issue_only_mode_blocked(self) -> None:
        safety = {"mode": "issue-only", "protected_branches": ["main"]}
        allowed, reason = check_pr_creation_allowed("main", safety)
        assert allowed is False
        assert "issue-only" in reason

    def test_issue_only_underscore_also_blocked(self) -> None:
        safety = {"mode": "issue_only"}
        allowed, _ = check_pr_creation_allowed("main", safety)
        assert allowed is False

    def test_pr_mode_allowed(self) -> None:
        safety = {"mode": "pr", "protected_branches": ["main"]}
        allowed, reason = check_pr_creation_allowed("main", safety)
        assert allowed is True
        assert reason == "ok"

    def test_merge_mode_allowed(self) -> None:
        safety = {"mode": "merge", "protected_branches": ["main"]}
        allowed, reason = check_pr_creation_allowed("main", safety)
        assert allowed is True
        assert reason == "ok"

    def test_no_safety_config_allows(self) -> None:
        allowed, reason = check_pr_creation_allowed("main", None)
        assert allowed is True
        assert reason == "no-safety-config"

    def test_empty_safety_config_allows(self) -> None:
        allowed, _ = check_pr_creation_allowed("main", {})
        assert allowed is True


# ---------------------------------------------------------------------------
# check_merge_allowed
# ---------------------------------------------------------------------------


class TestCheckMergeAllowed:
    def test_observe_mode_blocked(self) -> None:
        allowed, reason = check_merge_allowed({"mode": "observe"})
        assert allowed is False
        assert "observe" in reason

    def test_issue_only_mode_blocked(self) -> None:
        allowed, reason = check_merge_allowed({"mode": "issue-only"})
        assert allowed is False
        assert "issue-only" in reason

    def test_pr_mode_blocked(self) -> None:
        allowed, reason = check_merge_allowed({"mode": "pr"})
        assert allowed is False
        assert "pr mode" in reason
        assert "merge" in reason.lower()

    def test_merge_mode_allowed(self) -> None:
        allowed, reason = check_merge_allowed({"mode": "merge"})
        assert allowed is True
        assert reason == "ok"

    def test_no_safety_config_allows(self) -> None:
        allowed, reason = check_merge_allowed(None)
        assert allowed is True
        assert reason == "no-safety-config"

    def test_empty_safety_config_allows(self) -> None:
        allowed, _ = check_merge_allowed({})
        assert allowed is True

    def test_unknown_mode_blocked(self) -> None:
        # Merges fail closed for unrecognized modes (higher stakes).
        allowed, _ = check_merge_allowed({"mode": "yolo"})
        assert allowed is False


# ---------------------------------------------------------------------------
# resolve_base_branch
# ---------------------------------------------------------------------------


class TestResolveBaseBranch:
    def test_explicit_base_branch_override(self) -> None:
        safety = {"protected_branches": ["main"]}
        repo = {"base_branch": "develop"}
        assert resolve_base_branch(safety, repo) == "develop"

    def test_default_branch_override(self) -> None:
        safety = {"protected_branches": ["main"]}
        repo = {"default_branch": "trunk"}
        assert resolve_base_branch(safety, repo) == "trunk"

    def test_base_branch_takes_precedence_over_default_branch(self) -> None:
        safety = {"protected_branches": ["main"]}
        repo = {"base_branch": "develop", "default_branch": "trunk"}
        assert resolve_base_branch(safety, repo) == "develop"

    def test_falls_back_to_protected_branches_first_entry(self) -> None:
        safety = {"protected_branches": ["develop", "main"]}
        assert resolve_base_branch(safety, {}) == "develop"

    def test_falls_back_to_main_default(self) -> None:
        assert resolve_base_branch({}, {}) == DEFAULT_BASE_BRANCH
        assert DEFAULT_BASE_BRANCH == "main"

    def test_none_configs_fall_back_to_main(self) -> None:
        assert resolve_base_branch(None, None) == "main"

    def test_whitespace_trimmed_in_override(self) -> None:
        repo = {"base_branch": "  release-next  "}
        assert resolve_base_branch({}, repo) == "release-next"

    def test_empty_string_override_ignored(self) -> None:
        safety = {"protected_branches": ["develop"]}
        repo = {"base_branch": "  "}
        assert resolve_base_branch(safety, repo) == "develop"

    def test_empty_protected_branches_list_falls_back(self) -> None:
        safety = {"protected_branches": []}
        assert resolve_base_branch(safety, {}) == "main"


# ---------------------------------------------------------------------------
# Integration: create_or_update_github_pr
# ---------------------------------------------------------------------------


class TestCreateOrUpdateGithubPrSafetyGate:
    def test_observe_mode_blocks_pr_creation(
        self, tmp_path: Path, make_finding
    ) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        safety = {"mode": "observe", "protected_branches": ["main"]}

        with patch("bluei.engine.gh.find_existing_github_pr", return_value=None):
            with patch("bluei.engine.gh.run_capture") as mock_rc:
                result = create_or_update_github_pr(
                    "o/r",
                    finding,
                    "fix-branch",
                    None,
                    False,  # dry_run=False so we reach the gate
                    log,
                    tmp_path,
                    safety_config=safety,
                )
        # Gate should block before any gh call
        mock_rc.assert_not_called()
        assert result["created"] is False
        assert result["error"] == "blocked-by-safety-mode"
        assert result["number"] is None

    def test_issue_only_mode_blocks_pr_creation(
        self, tmp_path: Path, make_finding
    ) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        safety = {"mode": "issue-only"}

        with patch("bluei.engine.gh.find_existing_github_pr", return_value=None):
            with patch("bluei.engine.gh.run_capture") as mock_rc:
                result = create_or_update_github_pr(
                    "o/r",
                    finding,
                    "fix-branch",
                    None,
                    False,
                    log,
                    tmp_path,
                    safety_config=safety,
                )
        mock_rc.assert_not_called()
        assert result["created"] is False
        assert result["error"] == "blocked-by-safety-mode"

    def test_pr_mode_allows_creation(self, tmp_path: Path, make_finding) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        safety = {"mode": "pr", "protected_branches": ["main"]}
        url = "https://github.com/o/r/pull/20"

        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(0, url)),
        ):
            result = create_or_update_github_pr(
                "o/r",
                finding,
                "fix-branch",
                42,
                False,
                log,
                tmp_path,
                safety_config=safety,
            )
        assert result["created"] is True
        assert result["number"] == 20

    def test_no_safety_config_allows_creation(
        self, tmp_path: Path, make_finding
    ) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        url = "https://github.com/o/r/pull/21"

        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(0, url)),
        ):
            result = create_or_update_github_pr(
                "o/r", finding, "fix-branch", None, False, log, tmp_path
            )
        assert result["created"] is True

    def test_base_branch_override_used_in_create(
        self, tmp_path: Path, make_finding
    ) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        safety = {"mode": "merge", "protected_branches": ["main"]}
        repo = {"base_branch": "develop"}
        url = "https://github.com/o/r/pull/22"

        with (
            patch("bluei.engine.gh.find_existing_github_pr", return_value=None),
            patch("bluei.engine.gh.run_capture", return_value=(0, url)) as mock_rc,
        ):
            result = create_or_update_github_pr(
                "o/r",
                finding,
                "fix-branch",
                None,
                False,
                log,
                tmp_path,
                safety_config=safety,
                repo_config=repo,
            )
        assert result["created"] is True
        # Verify --base develop was used, not --base main
        cmd = mock_rc.call_args.args[0]
        assert "--base" in cmd
        idx = cmd.index("--base")
        assert cmd[idx + 1] == "develop"

    def test_existing_pr_reuse_unaffected_by_safety(
        self, tmp_path: Path, make_finding
    ) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        existing = {"number": 99, "url": "https://github.com/o/r/pull/99"}
        safety = {"mode": "observe"}  # would normally block

        with patch("bluei.engine.gh.find_existing_github_pr", return_value=existing):
            with patch("bluei.engine.gh.run_capture") as mock_rc:
                result = create_or_update_github_pr(
                    "o/r",
                    finding,
                    "fix-branch",
                    None,
                    False,
                    log,
                    tmp_path,
                    safety_config=safety,
                )
        # Reuse of existing PR is allowed even in observe mode
        assert result["created"] is False
        assert result["number"] == 99
        mock_rc.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: git_push_branch
# ---------------------------------------------------------------------------


class TestGitPushBranchSafetyGate:
    def test_push_to_main_blocked(self, tmp_path: Path) -> None:
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")
        safety = {"mode": "pr", "protected_branches": ["main", "master"]}

        with patch("bluei.engine.git_ops.run_capture") as mock_rc:
            result = git_push_branch(
                tmp_path, "main", log, dry_run=False, safety_config=safety
            )
        assert result is False
        mock_rc.assert_not_called()
        assert "safety-block" in log.read_text()

    def test_push_to_master_blocked(self, tmp_path: Path) -> None:
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")
        safety = {"mode": "merge", "protected_branches": ["main", "master"]}

        with patch("bluei.engine.git_ops.run_capture") as mock_rc:
            result = git_push_branch(
                tmp_path, "master", log, dry_run=False, safety_config=safety
            )
        assert result is False
        mock_rc.assert_not_called()

    def test_push_to_feature_branch_allowed(self, tmp_path: Path) -> None:
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")
        safety = {"mode": "pr", "protected_branches": ["main", "master"]}

        with patch("bluei.engine.git_ops.run_capture", return_value=(0, "pushed")):
            result = git_push_branch(
                tmp_path,
                "qa/fix-typo",
                log,
                dry_run=False,
                safety_config=safety,
            )
        assert result is True

    def test_push_to_main_allowed_without_safety_config(self, tmp_path: Path) -> None:
        """Backward compat: no safety_config means no enforcement."""
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")

        with patch("bluei.engine.git_ops.run_capture", return_value=(0, "pushed")):
            result = git_push_branch(tmp_path, "main", log, dry_run=False)
        assert result is True

    def test_safety_block_takes_precedence_over_dry_run(self, tmp_path: Path) -> None:
        """Even in dry-run, a protected push is blocked (reports the block)."""
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")
        safety = {"mode": "pr", "protected_branches": ["main"]}

        with patch("bluei.engine.git_ops.run_capture") as mock_rc:
            result = git_push_branch(
                tmp_path, "main", log, dry_run=True, safety_config=safety
            )
        assert result is False
        mock_rc.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: merge_pr
# ---------------------------------------------------------------------------


class TestMergePrSafetyGate:
    def test_observe_mode_blocks_merge(self, tmp_path: Path) -> None:
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            success, reason = merge_pr(
                "o/r",
                42,
                dry_run=False,
                cwd=tmp_path,
                safety_config={"mode": "observe"},
            )
        assert success is False
        assert "blocked-by-safety-mode" in reason
        mock_rc.assert_not_called()

    def test_pr_mode_blocks_merge(self, tmp_path: Path) -> None:
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            success, reason = merge_pr(
                "o/r", 42, dry_run=False, cwd=tmp_path, safety_config={"mode": "pr"}
            )
        assert success is False
        assert "blocked-by-safety-mode" in reason
        assert "pr mode" in reason
        mock_rc.assert_not_called()

    def test_merge_mode_allows_merge(self, tmp_path: Path) -> None:
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture", return_value=(0, "merged")):
            success, reason = merge_pr(
                "o/r",
                42,
                dry_run=False,
                cwd=tmp_path,
                safety_config={"mode": "merge"},
            )
        assert success is True
        assert reason == "merged"

    def test_no_safety_config_allows_merge(self, tmp_path: Path) -> None:
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture", return_value=(0, "merged")):
            success, _ = merge_pr("o/r", 42, dry_run=False, cwd=tmp_path)
        assert success is True

    def test_safety_block_takes_precedence_over_dry_run(self, tmp_path: Path) -> None:
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            success, reason = merge_pr(
                "o/r",
                42,
                dry_run=True,
                cwd=tmp_path,
                safety_config={"mode": "pr"},
            )
        assert success is False
        assert "blocked-by-safety-mode" in reason
        mock_rc.assert_not_called()
