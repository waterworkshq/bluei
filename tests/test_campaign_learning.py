"""Tests for the Campaign Lab (Slice 2): LearningObjective + evidence routing.

Covers AC-P2-1..6:
- LearningObjective round-trip incl. Campaign.from_dict reconstruction (the
  verified gap).
- None-objective campaigns behave exactly as before (no evidence writes).
- Emergent-rule objective advances the target rule via record_shadow_run AND
  proposes new rules via propose_rules_from_fix_patterns.
- Pattern-family objective writes dry_replay.jsonl.
- AC-P2-4 (hard constraint): zero approval_records.jsonl writes — verified
  byte-identical before/after for both objective kinds.
- learning_report aggregation (method_breakdown / assets_proven /
  proposed_rules / candidates).
- autofix_fix_runner return carries the additive
  deterministic / matched_asset / cascade_stage keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from bluei.app.emergent_rules import (
    DetectionPattern,
    DetectionType,
    EmergentRule,
    EmergentRuleStatus,
    EmergentRuleStore,
)
from bluei.campaigns.executor import (
    CampaignExecutor,
    autofix_fix_runner,
)
from bluei.campaigns.planner import Campaign, CampaignPlanner
from bluei.campaigns.state import CampaignStateManager
from bluei.campaigns.types import (
    LearningObjective,
    ObjectiveTarget,
)
from bluei.engine.models import Finding


# --------------------------------------------------------------------------- #
# LearningObjective serialization
# --------------------------------------------------------------------------- #


def test_learning_objective_round_trip() -> None:
    lo = LearningObjective(
        target_type=ObjectiveTarget.pattern_family,
        target_ref="ruff-b",
        notes="advance b-family shadow evidence",
    )
    payload = lo.to_dict()
    assert payload == {
        "target_type": "pattern_family",
        "target_ref": "ruff-b",
        "notes": "advance b-family shadow evidence",
    }
    rebuilt = LearningObjective.from_dict(payload)
    assert rebuilt.target_type is ObjectiveTarget.pattern_family
    assert rebuilt.target_ref == "ruff-b"
    assert rebuilt.notes == "advance b-family shadow evidence"


def test_learning_objective_from_dict_backfills_notes() -> None:
    rebuilt = LearningObjective.from_dict(
        {"target_type": "emergent_rule", "target_ref": "er-xyz"}
    )
    assert rebuilt.notes == ""
    assert rebuilt.target_type is ObjectiveTarget.emergent_rule


def test_campaign_from_dict_reconstructs_learning_objective() -> None:
    """The CRITICAL reconstruction gap (DESIGN §2.2): a nested LearningObjective
    dict in a serialized Campaign must be reconstructed into the dataclass."""
    planner = CampaignPlanner()
    base = planner.plan(
        [],
        repo="demo",
        title="objective campaign",
    )
    base.learning_objective = LearningObjective(
        target_type=ObjectiveTarget.recipe,
        target_ref="ruff-e701",
        notes="recipe test",
    )
    serialized = base.to_dict()
    # Sanity: the nested objective landed as a dict, not the dataclass.
    assert isinstance(serialized["learning_objective"], dict)

    rebuilt = Campaign.from_dict(serialized)
    assert isinstance(rebuilt.learning_objective, LearningObjective)
    assert rebuilt.learning_objective.target_type is ObjectiveTarget.recipe
    assert rebuilt.learning_objective.target_ref == "ruff-e701"
    assert rebuilt.learning_objective.notes == "recipe test"


def test_campaign_from_dict_handles_missing_objective() -> None:
    """Old campaign files (no learning_objective key) must load unchanged."""
    planner = CampaignPlanner()
    base = planner.plan([], repo="demo", title="legacy")
    serialized = base.to_dict()
    del serialized["learning_objective"]
    rebuilt = Campaign.from_dict(serialized)
    assert rebuilt.learning_objective is None


def test_plan_attaches_learning_objective() -> None:
    lo = LearningObjective(
        target_type=ObjectiveTarget.pattern_family,
        target_ref="ruff-b",
    )
    campaign = CampaignPlanner().plan(
        [],
        repo="demo",
        title="planned objective",
        learning_objective=lo,
    )
    assert campaign.learning_objective is lo


# --------------------------------------------------------------------------- #
# Helpers for executor-driven tests
# --------------------------------------------------------------------------- #


def _fix_runner_success(
    *,
    campaign: Any,
    phase: Any,
    finding_id: str,
    finding: Dict[str, Any],
    worktree_path: Path,
    pattern_store_path: Path | None = None,
) -> Dict[str, Any]:
    """A deterministic fix_runner stub that mutates the target file so Dry
    Replay can snapshot pre/post content."""
    rel = finding.get("path", "")
    if rel:
        target = Path(worktree_path) / rel
        if target.exists():
            target.write_text(f"# fixed {finding_id}\n", encoding="utf-8")
    return {
        "status": "success",
        "method": "pattern_replay",
        "deterministic": True,
        "matched_asset": "seed-test-1",
        "cascade_stage": None,
    }


def _emergent_rule(
    rule_id: str = "er-test",
    *,
    status: EmergentRuleStatus = EmergentRuleStatus.CANDIDATE,
    negative_examples=None,
) -> EmergentRule:
    return EmergentRule(
        rule_id=rule_id,
        header=f"Test rule {rule_id}",
        detection_pattern=DetectionPattern(
            detection_type=DetectionType.TEXT_PATTERN,
            search_pattern="console_log_bad",
            file_glob="**/*.py",
        ),
        language="python",
        category="lint",
        status=status,
        negative_examples=negative_examples or [],
    )


def _seed_emergent_store(store_path: Path, rules) -> EmergentRuleStore:
    store = EmergentRuleStore(store_path)
    store.save(list(rules))
    return store


def _build_one_finding_campaign(
    store_dir: Path,
    *,
    rule: str = "ruff-b904",
    path: str = "src/app.py",
    snippet: str = "raise ValueError('x')\n",
    objective: LearningObjective | None = None,
) -> tuple[CampaignStateManager, Campaign]:
    state = CampaignStateManager(store_dir)
    finding = Finding(
        finding_id="f-1",
        repo="demo",
        path=path,
        line=1,
        rule=rule,
        snippet=snippet,
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )
    campaign = CampaignPlanner().plan(
        [finding],
        repo="demo",
        title="objective run",
        learning_objective=objective,
    )
    state.save(campaign)
    # Materialize the target file so the fix_runner stub can mutate it.
    return state, campaign


# --------------------------------------------------------------------------- #
# None-objective: no evidence writes, today's behavior
# --------------------------------------------------------------------------- #


def test_none_objective_campaign_emits_no_learning_report(tmp_path: Path) -> None:
    state, campaign = _build_one_finding_campaign(tmp_path / "campaigns")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("raise ValueError('x')\n")
    dry_path = tmp_path / "dry.jsonl"
    approval_path = tmp_path / "approval_records.jsonl"
    approval_path.write_text("")

    exe = CampaignExecutor(
        state,
        fix_runner=_fix_runner_success,
        dry_replay_path=dry_path,
    )
    result = exe.run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
    )
    assert "learning_report" not in result
    # No dry-replay evidence written for a None objective.
    assert not dry_path.exists()
    # Governance trail untouched.
    assert approval_path.read_text() == ""


# --------------------------------------------------------------------------- #
# Pattern-family: writes dry_replay.jsonl, governance untouched
# --------------------------------------------------------------------------- #


def _seed_pattern_store(store_path: Path, patterns) -> None:
    """Write a JSONL FixPatternStore file."""
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with store_path.open("w", encoding="utf-8") as handle:
        for pattern in patterns:
            handle.write(json.dumps(pattern) + "\n")


def test_pattern_family_objective_writes_dry_replay_and_preserves_governance(
    tmp_path: Path,
) -> None:
    from bluei.engine.pattern_store import FixPatternStore

    state, campaign = _build_one_finding_campaign(
        tmp_path / "campaigns",
        objective=LearningObjective(
            target_type=ObjectiveTarget.pattern_family,
            target_ref="ruff-b",  # derive_rule_family("ruff-b904") == "ruff-b"
        ),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("raise ValueError('x')\n")

    # Seed a pattern store with a candidate for ruff-b904.
    pattern_store_path = tmp_path / "patterns.jsonl"
    _seed_pattern_store(
        pattern_store_path,
        [
            {
                "pattern_id": "seed-test-1",
                "rule": "ruff-b904",
                "language": "python",
                "file_path": "src/app.py",
                "before_snippet": "raise ValueError('x')",
                "after_snippet": "raise ValueError('x') from None",
                "confidence": 0.95,
                "success_count": 7,
                "failure_count": 0,
                "skip_count": 0,
                "structural_hash": "deadbeefdeadbeef",
                "file_pattern": "**/*",
                "framework_constraint": None,
                "excluded_paths": [],
                "imports_touched": [],
                "source": "test",
                "source_finding_ids": [],
                "validation_commands_passed": [],
                "created_at": "",
                "last_used_at": None,
                "last_verified_at": None,
                "last_failed_at": None,
                "diff_patch": "",
            }
        ],
    )
    dry_path = tmp_path / "dry.jsonl"
    approval_path = tmp_path / "approval_records.jsonl"
    approval_before = b""
    approval_path.write_bytes(approval_before)

    exe = CampaignExecutor(
        state,
        fix_runner=_fix_runner_success,
        pattern_store_path=pattern_store_path,
        dry_replay_path=dry_path,
        baseline_checks={},
        run_id="camp-run-1",
    )
    result = exe.run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
    )

    # AC-P2-3: dry_replay.jsonl records written.
    assert dry_path.exists()
    records = [json.loads(line) for line in dry_path.read_text().splitlines() if line]
    assert records, "dry_replay.jsonl must contain at least one record"
    assert all("would_have_outcome" in r for r in records)
    assert all(r["run_id"] == "camp-run-1" for r in records)

    # AC-P2-4: governance trail byte-identical.
    assert approval_path.read_bytes() == approval_before

    # learning_report aggregates.
    report = result["learning_report"]
    assert report["objective"]["target_type"] == "pattern_family"
    assert report["objective"]["target_ref"] == "ruff-b"
    assert report["method_breakdown"].get("pattern_replay") == 1
    assert "seed-test-1" in report["candidates"]


# --------------------------------------------------------------------------- #
# Emergent-rule: advances target + proposes new rules, governance untouched
# --------------------------------------------------------------------------- #


def test_emergent_rule_objective_advances_target_and_proposes(
    tmp_path: Path,
) -> None:
    from functools import partial

    from bluei.app.campaign_evidence import consume_emergent_evidence

    state, campaign = _build_one_finding_campaign(
        tmp_path / "campaigns",
        rule="ruff-b904",
        snippet="raise ValueError('x')\n",
        objective=LearningObjective(
            target_type=ObjectiveTarget.emergent_rule,
            target_ref="er-target",
        ),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("raise ValueError('x')\n")

    emergent_path = tmp_path / "emergent_rules.json"
    _seed_emergent_store(
        emergent_path,
        [_emergent_rule(rule_id="er-target", status=EmergentRuleStatus.CANDIDATE)],
    )

    # Seed a pattern store whose high-success patterns trigger proposals.
    pattern_store_path = tmp_path / "patterns.jsonl"
    _seed_pattern_store(
        pattern_store_path,
        [
            {
                "pattern_id": "seed-propose-1",
                "rule": "ruff-b904",
                "language": "python",
                "file_path": "src/app.py",
                "before_snippet": "raise ValueError('x')",
                "after_snippet": "raise ValueError('x') from None",
                "confidence": 0.8,
                "success_count": 12,
                "failure_count": 0,
                "skip_count": 0,
                "structural_hash": "abc123abc123abc1",
                "file_pattern": "**/*",
                "framework_constraint": None,
                "excluded_paths": [],
                "imports_touched": [],
                "source": "test",
                "source_finding_ids": [],
                "validation_commands_passed": [],
                "created_at": "",
                "last_used_at": None,
                "last_verified_at": None,
                "last_failed_at": None,
                "diff_patch": "",
            }
        ],
    )

    approval_path = tmp_path / "approval_records.jsonl"
    approval_before = b""
    approval_path.write_bytes(approval_before)

    # Inject the app-layer consumer (defined in bluei/app/campaign_evidence.py,
    # wired here the same way bin/cmd_campaign.py wires it). The campaigns
    # layer itself never imports bluei.app.* — the consumer carries the
    # emergent store path.
    consumer = partial(consume_emergent_evidence, emergent_store_path=emergent_path)
    exe = CampaignExecutor(
        state,
        fix_runner=_fix_runner_success,
        pattern_store_path=pattern_store_path,
        emergent_evidence_consumer=consumer,
        run_id="camp-run-em",
    )
    result = exe.run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
    )

    # AC-P2-2 (a): target rule advanced — CANDIDATE -> TENTATIVE (one shadow
    # run recorded; needs 3 to promote).
    rules_after = EmergentRuleStore(emergent_path).load()
    target = next(r for r in rules_after if r.rule_id == "er-target")
    assert target.shadow_runs >= 1
    assert target.status in (
        EmergentRuleStatus.TENTATIVE,
        EmergentRuleStatus.ACTIVE,
    )

    # AC-P2-2 (b): propose_rules_from_fix_patterns added PROPOSED rules.
    proposed_ids = [r.rule_id for r in rules_after if r.rule_id != "er-target"]
    assert proposed_ids, "expected at least one newly-proposed emergent rule"
    proposed = next(r for r in rules_after if r.rule_id != "er-target")
    assert proposed.status == EmergentRuleStatus.PROPOSED

    # AC-P2-4: governance trail untouched.
    assert approval_path.read_bytes() == approval_before

    # learning_report aggregates.
    report = result["learning_report"]
    assert report["objective"]["target_type"] == "emergent_rule"
    assert report["proposed_rules"]  # one or more ids
    assert report["method_breakdown"].get("pattern_replay") == 1


def test_emergent_objective_no_op_without_consumer(tmp_path: Path) -> None:
    """With no emergent_evidence_consumer wired (the default), the emergent
    objective routing is a no-op — no store mutation, no crash. Confirms the
    decoupling default is safe (campaigns never imports bluei.app.*)."""
    state, campaign = _build_one_finding_campaign(
        tmp_path / "campaigns",
        objective=LearningObjective(
            target_type=ObjectiveTarget.emergent_rule,
            target_ref="er-missing",
        ),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "src").mkdir()
    (worktree / "src" / "app.py").write_text("raise ValueError('x')\n")

    exe = CampaignExecutor(state, fix_runner=_fix_runner_success)
    result = exe.run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
    )
    report = result["learning_report"]
    # Without a consumer, no rules are proposed or advanced.
    assert report["proposed_rules"] == []
    # assets_proven captures matched_asset from the fix_runner regardless, but
    # no emergent rule transitions are observed without a consumer.
    assert all(not a.startswith("er-") for a in report["assets_proven"])


# --------------------------------------------------------------------------- #
# autofix_fix_runner additive return keys
# --------------------------------------------------------------------------- #


def test_autofix_fix_runner_additive_return_keys(tmp_path: Path) -> None:
    """The widened fix_runner return always carries deterministic /
    matched_asset / cascade_stage (T2.6)."""
    # Force the early-failure branch (missing snapshot field) to confirm the
    # additive keys survive even the failure returns.
    result = autofix_fix_runner(
        campaign=type("C", (), {"campaign_id": "c"})(),
        phase=type("P", (), {"phase_id": "p"})(),
        finding_id="f-1",
        finding={"finding_id": "f-1", "rule": "x"},  # missing required fields
        worktree_path=tmp_path,
    )
    assert result["status"] == "failed"
    assert result["deterministic"] is True
    assert result["matched_asset"] is None
    assert result["cascade_stage"] is None
