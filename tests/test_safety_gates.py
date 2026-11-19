"""Tests for the runtime safety gates (F1: protected-branch enforcement).

Covers:

- ``is_protected_branch`` — branch matching incl. case/whitespace
- ``check_push_allowed`` — protected-branch + care-mode push gate
- ``check_pr_creation_allowed`` — safety-mode gate for PR creation
- ``check_merge_allowed`` — safety-mode gate for merges
- ``resolve_base_branch`` — base-branch resolution precedence
- Fail-closed policy: missing/empty/invalid/non-string modes block every gate
- Care-form normalization: canonical/brand/underscore spellings behave alike
- Integration: ``create_or_update_github_pr`` blocked in OBSERVE mode
- Integration: ``git_push_branch`` blocked for protected branch + care modes
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

    def test_no_safety_config_blocks_fail_closed(self) -> None:
        allowed, reason = check_push_allowed("main", None)
        assert allowed is False
        assert "fail closed" in reason

    def test_empty_safety_config_blocks_fail_closed(self) -> None:
        allowed, _ = check_push_allowed("main", {})
        assert allowed is False

    def test_missing_mode_blocks_fail_closed(self) -> None:
        allowed, reason = check_push_allowed("qa/fix", {"protected_branches": ["main"]})
        assert allowed is False
        assert "fail closed" in reason

    def test_non_string_mode_blocks_fail_closed(self) -> None:
        # A non-string mode is invalid — never coerced via str().
        for bad in (["pr"], 42, True, None, {"mode": "pr"}):
            allowed, reason = check_push_allowed("qa/fix", {"mode": bad})
            assert allowed is False, bad
            assert "fail closed" in reason

    def test_unrecognized_mode_blocks_fail_closed(self) -> None:
        allowed, reason = check_push_allowed("qa/fix", {"mode": "yolo"})
        assert allowed is False
        assert "fail closed" in reason

    def test_observe_mode_blocks_feature_branch_push(self) -> None:
        # Restrictive care blocks ALL pushes — including pushes to a feature
        # branch that would update an existing PR's head.
        for mode in ("observe", "watch-only", "watch only"):
            allowed, reason = check_push_allowed("qa/fix-123", {"mode": mode})
            assert allowed is False, mode
            assert "observe mode forbids push" in reason

    def test_issue_only_mode_blocks_feature_branch_push(self) -> None:
        for mode in ("issue-only", "issue_only", "note-only", "note only"):
            allowed, reason = check_push_allowed("qa/fix-123", {"mode": mode})
            assert allowed is False, mode
            assert "issue-only mode forbids push" in reason

    def test_brand_permissive_modes_allow_feature_branch_push(self) -> None:
        for mode in ("pr", "offer-fixes", "offer fixes", "merge", "full-care", "full care"):
            allowed, _ = check_push_allowed("qa/fix-123", {"mode": mode})
            assert allowed is True, mode

    def test_block_push_flag_blocks_all_branches(self) -> None:
        allowed, reason = check_push_allowed(
            "qa/fix-123", {"mode": "merge", "block_push": True}
        )
        assert allowed is False
        assert "block" in reason or "forbids push" in reason

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

    def test_brand_restrictive_forms_blocked(self) -> None:
        for mode in ("watch-only", "watch only", "note-only", "note only"):
            allowed, _ = check_pr_creation_allowed("main", {"mode": mode})
            assert allowed is False, mode

    def test_pr_mode_allowed(self) -> None:
        safety = {"mode": "pr", "protected_branches": ["main"]}
        allowed, reason = check_pr_creation_allowed("main", safety)
        assert allowed is True
        assert reason == "ok"

    def test_brand_permissive_aliases_allowed(self) -> None:
        for mode in ("offer-fixes", "offer fixes", "full-care", "full care", "PR", "Merge"):
            allowed, _ = check_pr_creation_allowed("main", {"mode": mode})
            assert allowed is True, mode

    def test_merge_mode_allowed(self) -> None:
        safety = {"mode": "merge", "protected_branches": ["main"]}
        allowed, reason = check_pr_creation_allowed("main", safety)
        assert allowed is True
        assert reason == "ok"

    def test_no_safety_config_blocks_fail_closed(self) -> None:
        allowed, reason = check_pr_creation_allowed("main", None)
        assert allowed is False
        assert "fail closed" in reason

    def test_empty_safety_config_blocks_fail_closed(self) -> None:
        allowed, _ = check_pr_creation_allowed("main", {})
        assert allowed is False

    def test_missing_or_non_string_mode_blocks_fail_closed(self) -> None:
        for safety in ({"protected_branches": ["main"]}, {"mode": ["pr"]}, {"mode": None}, {"mode": 7}):
            allowed, reason = check_pr_creation_allowed("main", safety)
            assert allowed is False, safety
            assert "fail closed" in reason

    def test_unrecognized_mode_blocks_fail_closed(self) -> None:
        # Previously unknown modes allowed PR creation by default; the
        # approved policy supersedes that fail-open behaviour.
        allowed, reason = check_pr_creation_allowed("main", {"mode": "yolo"})
        assert allowed is False
        assert "fail closed" in reason


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

    def test_brand_care_forms_behave_like_canonical(self) -> None:
        assert check_merge_allowed({"mode": "full-care"}) == (True, "ok")
        assert check_merge_allowed({"mode": "full care"}) == (True, "ok")
        allowed, reason = check_merge_allowed({"mode": "offer-fixes"})
        assert allowed is False
        assert "pr mode" in reason  # offer-fixes IS the pr care level
        assert check_merge_allowed({"mode": "note-only"})[0] is False
        assert check_merge_allowed({"mode": "note only"})[0] is False
        assert check_merge_allowed({"mode": "issue_only"})[0] is False

    def test_no_safety_config_blocks_fail_closed(self) -> None:
        allowed, reason = check_merge_allowed(None)
        assert allowed is False
        assert "fail closed" in reason

    def test_empty_safety_config_blocks_fail_closed(self) -> None:
        allowed, _ = check_merge_allowed({})
        assert allowed is False

    def test_missing_or_non_string_mode_blocks_fail_closed(self) -> None:
        for safety in ({"protected_branches": ["main"]}, {"mode": ["merge"]}, {"mode": None}):
            allowed, reason = check_merge_allowed(safety)
            assert allowed is False, safety
            assert "fail closed" in reason

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


class TestResolveBaseBranchRepoConfigShape:
    """D7-C-1 regression: production passes a RepoConfig DATACLASS (stored on
    RunContext.repo_config at pr_cycle cycle-start), not a dict. The dataclass
    has no ``.get`` and no ``base_branch``/``default_branch`` fields — the old
    dict-only lookup raised AttributeError at the first live PR creation.
    resolve_base_branch must accept both shapes.
    """

    def _repo_config(self, **safety) -> "RepoConfig":
        from bluei.common.models import RepoConfig

        base_safety = {"mode": "pr", "protected_branches": ["main", "master"]}
        base_safety.update(safety)
        return RepoConfig(
            id="repo-1",
            name="demo",
            path="/tmp/demo",
            language="python",
            safety=base_safety,
        )

    def test_dataclass_no_attribute_error_resolves_via_safety(self) -> None:
        # Exact reviewer repro: a RepoConfig dataclass reaches the gate.
        # RepoConfig carries no base_branch/default_branch fields, so the
        # resolution falls through to safety.protected_branches[0].
        cfg = self._repo_config(protected_branches=["develop", "main"])
        resolved = resolve_base_branch(cfg.safety, cfg)
        assert resolved == "develop"

    def test_dataclass_defaults_fall_back_to_main(self) -> None:
        cfg = self._repo_config()  # protected_branches ["main", "master"]
        assert resolve_base_branch(cfg.safety, cfg) == "main"

    def test_dataclass_with_no_safety_config_falls_back_to_main(self) -> None:
        cfg = self._repo_config()
        assert resolve_base_branch(None, cfg) == "main"

    def test_dict_shape_behaviour_unchanged(self) -> None:
        safety = {"protected_branches": ["main"]}
        repo = {"base_branch": "develop"}
        assert resolve_base_branch(safety, repo) == "develop"

    def test_dataclass_and_its_dict_projection_agree(self) -> None:
        # The dataclass path and its to_dict() projection resolve identically
        # (the dataclass simply has no base-branch override fields).
        cfg = self._repo_config(protected_branches=["release", "main"])
        assert resolve_base_branch(cfg.safety, cfg) == resolve_base_branch(
            cfg.safety, cfg.to_dict()
        )


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

    def test_no_safety_config_blocks_creation_fail_closed(
        self, tmp_path: Path, make_finding
    ) -> None:
        """Approved policy supersedes the old None-allows contract: a missing
        runtime policy must block PR creation.

        NOTE: requires the caller-side ``if safety_config:`` guard in
        ``gh/pr_ops.py:create_or_update_github_pr`` to be removed/lifted by
        the integration owner — until then this test is red by design.
        """
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()

        with patch("bluei.engine.gh.find_existing_github_pr", return_value=None):
            with patch("bluei.engine.gh.run_capture") as mock_rc:
                result = create_or_update_github_pr(
                    "o/r", finding, "fix-branch", None, False, log, tmp_path
                )
        mock_rc.assert_not_called()
        assert result["created"] is False
        assert result["error"] == "blocked-by-safety-mode"

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

    def test_existing_pr_reuse_performs_no_gh_mutation(
        self, tmp_path: Path, make_finding
    ) -> None:
        from bluei.engine.gh import create_or_update_github_pr

        log = tmp_path / "test.log"
        log.touch()
        finding = make_finding()
        existing = {"number": 99, "url": "https://github.com/o/r/pull/99"}
        safety = {"mode": "observe"}

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
        # The reuse short-circuit performs no gh mutation, so the boundary
        # (no push/create/merge) holds even under a restrictive policy. If
        # the caller is later changed to route existing-PR *updates* through
        # check_pr_creation_allowed, this expectation must flip to blocked.
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

    def test_push_without_safety_config_blocks_fail_closed(self, tmp_path: Path) -> None:
        """Approved policy supersedes the old None-allows contract: a missing
        runtime policy must block the push.

        NOTE: requires the caller-side ``if safety_config:`` guard in
        ``git_ops.py:git_push_branch`` to be removed/lifted by the
        integration owner — until then this test is red by design.
        """
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")

        with patch("bluei.engine.git_ops.run_capture") as mock_rc:
            result = git_push_branch(tmp_path, "main", log, dry_run=False)
        assert result is False
        mock_rc.assert_not_called()
        assert "safety-block" in log.read_text()

    def test_observe_mode_blocks_feature_branch_push(self, tmp_path: Path) -> None:
        """Restrictive care blocks pushes that would update an existing PR's
        head branch (feature branch), not just protected-branch pushes."""
        from bluei.engine.git_ops import git_push_branch

        log = tmp_path / "log.txt"
        log.write_text("")
        safety = {"mode": "observe", "protected_branches": ["main", "master"]}

        with patch("bluei.engine.git_ops.run_capture") as mock_rc:
            result = git_push_branch(
                tmp_path, "qa/fix-typo", log, dry_run=False, safety_config=safety
            )
        assert result is False
        mock_rc.assert_not_called()
        assert "observe mode forbids push" in log.read_text()

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

    def test_no_safety_config_blocks_merge_fail_closed(self, tmp_path: Path) -> None:
        """Approved policy supersedes the old None-allows contract: a missing
        runtime policy must block the merge.

        NOTE: requires the caller-side ``if safety_config:`` guard in
        ``gh/pr_ops.py:merge_pr`` to be removed/lifted by the integration
        owner — until then this test is red by design.
        """
        from bluei.engine.gh import merge_pr

        with patch("bluei.engine.gh.run_capture") as mock_rc:
            success, reason = merge_pr("o/r", 42, dry_run=False, cwd=tmp_path)
        assert success is False
        assert "blocked-by-safety-mode" in reason
        mock_rc.assert_not_called()

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
