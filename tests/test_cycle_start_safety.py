"""Tests for the cycle-start safety-config projection (D7-H1 fix).

Exercises the shared production projection and the merge-only phase,
including policy enforcement, injected config, and configuration failures.

Before this projection existed, ``getattr(ctx, "safety_config", None)`` in
``_process_one_issue`` / ``run_merge_cycle_phase`` was structurally always
None (RunContext had no such field and nothing ever set it), so the
protected-branch / safety-mode gates never saw a policy and defaulted
permissive.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bluei.app.config import ConfigManager
from bluei.app.models import RepoConfig
from bluei.engine.commands.context import RunContext


def _resolve_cycle_configs(workspace: Path, repo_name: str):
    """Exercise the production cycle-start projection in a temporary workspace."""
    from bluei.engine.commands.pr_cycle import _project_repo_safety

    ctx = RunContext(args=None, repo_path=workspace / "repos" / repo_name)
    with patch("bluei.app.config.WORKSPACE", workspace):
        _project_repo_safety(ctx)
    return ctx.repo_config, ctx.safety_config


class TestRunContextSafetyField:
    def test_default_safety_config_is_none(self):
        ctx = RunContext(args=None)
        assert ctx.safety_config is None

    def test_default_repo_config_is_none(self):
        ctx = RunContext(args=None)
        assert ctx.repo_config is None


class TestCycleStartSafetyProjection:
    def test_config_with_safety_projects_the_dict(self, tmp_path):
        safety = {
            "mode": "pr",
            "protected_branches": ["develop", "main"],
            "require_clean_worktree": True,
        }
        cm = ConfigManager(tmp_path)
        cm.save_repo_config(
            RepoConfig(
                id="repo-demo",
                name="demo",
                path=str(tmp_path / "demo"),
                language="python",
                safety=safety,
            )
        )

        repo_config, safety_config = _resolve_cycle_configs(tmp_path, "demo")

        # repo_config is the DATACLASS (what the cycle stores)...
        assert isinstance(repo_config, RepoConfig)
        # ...and safety_config is its safety dict, not the dataclass itself.
        assert safety_config == safety
        assert isinstance(safety_config, dict)

    def test_missing_repo_config_projects_none(self, tmp_path):
        repo_config, safety_config = _resolve_cycle_configs(tmp_path, "absent")
        assert repo_config is None
        assert safety_config is None

    def test_projection_matches_seam_semantics_on_ctx(self, tmp_path):
        """The lifted value lands on the same field the gates read."""
        safety = {"mode": "merge", "protected_branches": ["main"]}
        cm = ConfigManager(tmp_path)
        cm.save_repo_config(
            RepoConfig(
                id="repo-demo",
                name="demo",
                path=str(tmp_path / "demo"),
                language="python",
                safety=safety,
            )
        )

        ctx = RunContext(args=None, repo_path=tmp_path / "demo")
        from bluei.engine.commands.pr_cycle import _project_repo_safety

        with patch("bluei.app.config.WORKSPACE", tmp_path):
            _project_repo_safety(ctx)

        # What _process_one_issue / merge_cycle read via getattr(ctx, ...):
        assert getattr(ctx, "safety_config", None) == safety


class TestPrCreationPathWithRepoConfig:
    """D7-C-1 end-of-chain regression: the live PR-creation path receives the
    RepoConfig DATACLASS from RunContext.repo_config. Pre-fix this raised
    AttributeError inside resolve_base_branch (``RepoConfig.get`` does not
    exist); the crash aborted the whole run at first PR creation.
    """

    def test_dry_run_pr_creation_with_dataclass_does_not_raise(
        self, tmp_path, make_finding
    ):
        from bluei.engine.gh import create_or_update_github_pr

        safety = {"mode": "pr", "protected_branches": ["develop", "main"]}
        repo_config = RepoConfig(
            id="repo-demo",
            name="demo",
            path=str(tmp_path / "demo"),
            language="python",
            safety=safety,
        )
        log = tmp_path / "test.log"
        log.touch()

        with patch("bluei.engine.gh.find_existing_github_pr", return_value=None):
            result = create_or_update_github_pr(
                "o/r",
                make_finding(),
                "qa/fix-branch",
                None,
                True,  # dry_run — resolve_base_branch runs BEFORE the dry-run exit
                log,
                tmp_path,
                safety_config=safety,
                repo_config=repo_config,
            )

        assert result == {"number": None, "url": "", "created": True}
        # The dataclass has no base-branch override fields, so the base came
        # from the safety dict's protected_branches[0].
        assert "base=develop" in log.read_text()


class TestMergeOnlySafetyProjection:
    @pytest.mark.parametrize("mode", ["observe", "pr", "merge"])
    def test_merge_cycle_ctx_gets_safety_without_pr_phase(self, tmp_path, mode):
        from bluei.engine.commands import merge_cycle as mc
        from bluei.engine.safety_gates import check_merge_allowed

        safety = {"mode": mode, "protected_branches": ["develop"]}
        ConfigManager(tmp_path).save_repo_config(RepoConfig(
            id="demo", name="demo", path=str(tmp_path / "demo"),
            language="python", safety=safety,
        ))
        ctx = RunContext(
            args=SimpleNamespace(
                auto_merge_sandbox=True, merge_cooldown_minutes=0,
                dry_run=False, auto_rebase_enabled=False, live_github_actions=False,
            ),
            repo_path=tmp_path / "repos" / "demo",
            log_file=tmp_path / "cycle.log",
            review_state_file=tmp_path / "review.json",
            gh_repo_slug="acme/sandbox-demo", open_prs=1,
        )
        pr = {"number": 7, "url": "https://github.com/acme/sandbox-demo/pull/7"}
        with (
            patch("bluei.app.config.WORKSPACE", tmp_path),
            patch.object(mc, "repo_is_sandbox", return_value=True),
            patch.object(mc, "fetch_open_prs_for_merge", return_value=[pr]),
            patch.object(mc, "evaluate_pr_check_health", return_value={"eligible": True, "has_checks": True}),
            patch.object(mc, "evaluate_pr_reviews", return_value={"eligible": True}),
            patch.object(mc, "evaluate_pr_mergeability", return_value={"eligible": True}),
            patch.object(mc, "reconcile_open_workload", return_value=(0, 0, {})),
            patch("bluei.engine.gh.pr_ops.check_merge_allowed", wraps=check_merge_allowed) as gate,
            patch("bluei.engine.gh.run_capture", return_value=(0, "")) as gh,
        ):
            result = mc.run_merge_cycle_phase(ctx)

        assert ctx.safety_config == safety
        gate.assert_called_once_with(safety)
        if mode == "merge":
            assert result[1] == 1
            gh.assert_called_once_with(
                ["gh", "pr", "merge", "7", "--repo", ctx.gh_repo_slug, "--merge", "--delete-branch"],
                cwd=ctx.repo_path,
            )
        else:
            assert result[0] == 1
            assert result[1] == 0
            assert any("blocked-by-safety-mode" in reason for reason in result[6])
            gh.assert_not_called()

    @pytest.mark.parametrize("supplied", ["repo", "safety", "both", "empty-safety"])
    def test_injected_config_is_not_reloaded(self, tmp_path, supplied):
        from bluei.engine.commands.pr_cycle import _project_repo_safety

        safety = {"mode": "observe"}
        repo = RepoConfig(id="demo", name="demo", path=str(tmp_path), language="python", safety=safety)
        ctx = RunContext(args=None, repo_path=tmp_path / "demo")
        if supplied in ("repo", "both"):
            ctx.repo_config = repo
        if supplied in ("safety", "both", "empty-safety"):
            ctx.safety_config = {} if supplied == "empty-safety" else {"mode": "pr"}
        original = ctx.safety_config
        with patch("bluei.app.config.ConfigManager", side_effect=OSError("unreadable")) as manager:
            _project_repo_safety(ctx)
        manager.assert_not_called()
        assert ctx.repo_config is (repo if supplied in ("repo", "both") else None)
        assert ctx.safety_config is (safety if supplied == "repo" else original)

    @pytest.mark.parametrize("contents", [
        "[invalid", "[]", "name: demo\n",
        "id: demo\nname: demo\npath: /demo\nlanguage: python\nsafety:\n  mode: invalid\n",
    ])
    def test_invalid_existing_config_fails_closed(self, tmp_path, contents):
        path = ConfigManager(tmp_path).get_repo_config_path("demo")
        path.parent.mkdir(parents=True)
        path.write_text(contents)
        with pytest.raises((ValueError, TypeError, AttributeError)):
            _resolve_cycle_configs(tmp_path, "demo")

    def test_unreadable_config_fails_closed(self, tmp_path):
        with patch("bluei.app.config.ConfigManager.load_repo_config", side_effect=PermissionError("unreadable")):
            with pytest.raises(PermissionError, match="unreadable"):
                _resolve_cycle_configs(tmp_path, "demo")
