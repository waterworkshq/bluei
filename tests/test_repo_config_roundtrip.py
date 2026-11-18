#!/usr/bin/env python3
"""A08: accepted/generated/documented configs round-trip into executable policy.

RepoConfig.to_dict() → RepoConfig.from_dict() must preserve every field;
unsupported top-level keys must fail loud (ValueError naming the key, not a
TypeError from the constructor or a silent drop); profile limit presets must
stay inside the engine hard cap (max_prs_per_run 1-2).
"""

from dataclasses import fields

import pytest

from bluei.app.models import RepoConfig
from bluei.app.onboarding.inference import apply_safety_profile


def _full_config() -> RepoConfig:
    # review_care must be the FULL default shape: from_dict's per-key
    # setdefaults would otherwise grow a partial dict on revival (existing
    # migration semantics, not a product bug). Start from the dataclass
    # default copy, then set the fields this fixture cares about.
    import copy

    review_care = copy.deepcopy(
        RepoConfig(id="d", name="d", path="/d", language="python").review_care
    )
    review_care.update(
        {"enabled": True, "mode": "observation", "guarded_live_review": False}
    )
    return RepoConfig(
        id="repo-roundtrip",
        name="roundtrip",
        path="/tmp/roundtrip",
        language="python",
        framework="flask",
        rule_pack="operator-pack",
        enabled=False,
        plugin_id="plugin-python",
        discovery={"ecosystem": "python", "monorepo": False},
        rules_enabled=["R1"],
        rules_disabled=["R2"],
        fix_engine="claude",
        fallback_engines=["claude", "deterministic"],
        claude_template="claude {prompt_file}",
        opencode_template="opencode run {prompt_file}",
        review_claude_template="claude review {pr}",
        review_opencode_template="opencode review {pr}",
        baseline_checks=[
            ["/opt/private/validate.sh", "--strict"],
            ["/abs/venv/bin/python", "-m", "pytest", "-q"],
        ],
        limits={
            "open_issues_cap": 11,
            "open_prs_cap": 3,
            "max_prs_per_run": 1,
            "max_issues_per_run": 4,
            "max_files_changed": 2,
            "max_loc_diff": 50,
            "max_fix_attempts": 1,
        },
        cooldowns={"finding_seconds": 100, "merge_minutes": 5, "staleness_seconds": 60},
        github={"live_actions": True, "auto_merge": False},
        review_care=review_care,
        safety={
            "mode": "pr",
            "profile": "balanced",
            "require_clean_worktree": True,
            "protected_branches": ["trunk"],
            "allow_live_on_dirty_tree": False,
            "notes": ["operator"],
        },
        meta={"onboarding_version": 2, "template": "python-api", "inferred_by": "test"},
        notifications={
            "enabled": False,
            "channels": [],
            "digest": {"enabled": False, "schedule": "never", "channels": []},
            "rate_limit": {"cooldown_seconds": 300, "max_per_hour": 20},
        },
    )


class TestRoundTrip:
    def test_full_field_round_trip_preserves_every_dataclass_field(self):
        cfg = _full_config()
        revived = RepoConfig.from_dict(cfg.to_dict())
        for f in fields(RepoConfig):
            assert getattr(revived, f.name) == getattr(cfg, f.name), f.name

    def test_operator_absolute_commands_survive_round_trip(self):
        revived = RepoConfig.from_dict(_full_config().to_dict())
        assert revived.baseline_checks[0] == ["/opt/private/validate.sh", "--strict"]
        assert revived.baseline_checks[1] == [
            "/abs/venv/bin/python",
            "-m",
            "pytest",
            "-q",
        ]


class TestUnsupportedTopLevelKeys:
    def test_documented_learning_key_is_rejected_loud(self):
        # Stage 1 corrected the docs to workspace-only learning; a stale
        # per-repo ``learning`` key must fail loud instead of TypeError.
        data = _full_config().to_dict()
        data["learning"] = {"mode": "per-repo"}
        with pytest.raises(ValueError, match="unsupported top-level key.*learning"):
            RepoConfig.from_dict(data)

    def test_all_unknown_keys_are_named(self):
        data = _full_config().to_dict()
        data["future_key"] = 1
        data["another_typo"] = 2
        with pytest.raises(ValueError) as excinfo:
            RepoConfig.from_dict(data)
        assert "future_key" in str(excinfo.value)
        assert "another_typo" in str(excinfo.value)


class TestProfileLimitsRoundTrip:
    @pytest.mark.parametrize("profile", ["conservative", "balanced", "aggressive"])
    def test_profiles_round_trip_within_engine_hard_cap(self, profile):
        cfg = _full_config()
        cfg.safety = {
            "mode": "pr",
            "profile": profile,
            "require_clean_worktree": True,
            "protected_branches": ["main"],
            "allow_live_on_dirty_tree": False,
            "notes": [],
        }
        cfg = apply_safety_profile(cfg)
        # engine/commands/early_exits.py hard-locks max-prs-per-run to 1-2
        assert cfg.limits["max_prs_per_run"] in (1, 2)
        revived = RepoConfig.from_dict(cfg.to_dict())
        assert revived.limits == cfg.limits

    def test_aggressive_profile_hits_hard_cap_exactly(self):
        cfg = _full_config()
        cfg.safety = {"mode": "pr", "profile": "aggressive"}
        cfg = apply_safety_profile(cfg)
        assert cfg.limits["max_prs_per_run"] == 2
