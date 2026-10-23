"""Tests for the cycle-start safety-config projection (D7-H1 fix).

Covers the construction seam in ``run_pr_cycle_phase`` (pr_cycle.py, right
next to the ``ctx.repo_config`` resolution):

    ctx.repo_config = ConfigManager().load_repo_config(ctx.repo_path.name)
    ctx.safety_config = ctx.repo_config.safety if ctx.repo_config else None

Before this projection existed, ``getattr(ctx, "safety_config", None)`` in
``_process_one_issue`` / ``run_merge_cycle_phase`` was structurally always
None (RunContext had no such field and nothing ever set it), so the
protected-branch / safety-mode gates never saw a policy and defaulted
permissive.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bluei.app.config import ConfigManager
from bluei.app.models import RepoConfig
from bluei.engine.commands.context import RunContext


def _resolve_cycle_configs(workspace: Path, repo_name: str):
    """Replicate pr_cycle.py's cycle-start construction seam verbatim."""
    from bluei.app.config import ConfigManager

    repo_config = ConfigManager(workspace).load_repo_config(repo_name)
    safety_config = repo_config.safety if repo_config else None
    return repo_config, safety_config


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
        # The exact two lines from pr_cycle cycle-start:
        ctx.repo_config = ConfigManager(tmp_path).load_repo_config(
            ctx.repo_path.name
        )
        ctx.safety_config = ctx.repo_config.safety if ctx.repo_config else None

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
