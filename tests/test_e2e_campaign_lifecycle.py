#!/usr/bin/env python3
"""E2E campaign lifecycle tests — plan, save, dry-run, state transitions."""

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from bluei.campaigns.executor import CampaignExecutor
from bluei.campaigns.planner import Campaign, CampaignPhase, CampaignPlanner
from bluei.campaigns.state import CampaignStateManager
from bluei.campaigns.types import CampaignStatus, CampaignStrategy, PhaseStatus
from bluei.engine.models import Finding


class TestCampaignPlanning:
    def test_plan_groups_findings_by_rule(self, make_app_finding):
        findings = [
            make_app_finding(
                finding_id="f-1",
                rule="ruff-b007",
                path="a.py",
                line=1,
                repo="test/repo",
                confidence=0.9,
            ),
            make_app_finding(
                finding_id="f-2",
                rule="ruff-b007",
                path="b.py",
                line=5,
                repo="test/repo",
                confidence=0.9,
            ),
            make_app_finding(
                finding_id="f-3",
                rule="broad-except",
                path="c.py",
                line=10,
                repo="test/repo",
                confidence=0.9,
            ),
        ]

        campaign = CampaignPlanner().plan(
            findings,
            repo="test/repo",
            title="Test campaign",
        )

        assert campaign.status == CampaignStatus.planning
        assert len(campaign.phases) >= 1
        assert campaign.total_findings == 3

    def test_plan_empty_findings_produces_zero_phases(self):
        campaign = CampaignPlanner().plan(
            [],
            repo="test/repo",
            title="Empty campaign",
        )
        assert len(campaign.phases) == 0
        assert campaign.total_findings == 0

    def test_plan_with_target_rules_filter(self, make_app_finding):
        findings = [
            make_app_finding(
                finding_id="f-1", rule="ruff-b007", repo="test/repo", confidence=0.9
            ),
            make_app_finding(
                finding_id="f-2", rule="broad-except", repo="test/repo", confidence=0.9
            ),
        ]

        campaign = CampaignPlanner().plan(
            findings,
            repo="test/repo",
            title="Filtered campaign",
            target_rules=["ruff-b007"],
        )

        all_finding_ids = []
        for phase in campaign.phases:
            all_finding_ids.extend(phase.finding_ids)
        assert "f-1" in all_finding_ids
        assert "f-2" not in all_finding_ids or campaign.status == CampaignStatus.aborted


class TestCampaignStatePersistence:
    def test_save_and_load(self, tmp_path, make_app_finding):
        store = CampaignStateManager(tmp_path)

        findings = [
            make_app_finding(
                finding_id="f-1", rule="ruff-b007", repo="test/repo", confidence=0.9
            )
        ]
        campaign = CampaignPlanner().plan(
            findings,
            repo="test/repo",
            title="Persist test",
        )
        store.save(campaign)

        loaded = store.load(campaign.campaign_id)
        assert loaded.campaign_id == campaign.campaign_id
        assert loaded.repo == "test/repo"

    def test_list_campaigns(self, tmp_path, make_app_finding):
        store = CampaignStateManager(tmp_path)

        for i in range(3):
            findings = [
                make_app_finding(
                    finding_id=f"f-{i}",
                    rule=f"rule-{i}",
                    repo="test/repo",
                    confidence=0.9,
                )
            ]
            c = CampaignPlanner().plan(
                findings,
                repo="test/repo",
                title=f"Campaign {i}",
            )
            store.save(c)

        campaigns = store.list()
        assert len(campaigns) == 3

    def test_events_append_and_read(self, tmp_path, make_app_finding):
        store = CampaignStateManager(tmp_path)

        findings = [
            make_app_finding(
                finding_id="f-1", rule="ruff-b007", repo="test/repo", confidence=0.9
            )
        ]
        c = CampaignPlanner().plan(findings, repo="test/repo", title="Events test")
        store.save(c)

        store.append_event(c.campaign_id, "campaign.created", {"repo": "test/repo"})
        store.append_event(c.campaign_id, "phase.started", {"phase": 0})

        events = store.events(c.campaign_id)
        assert len(events) == 2
        assert events[0]["event"] == "campaign.created"
        assert events[1]["event"] == "phase.started"

        last = store.last_event(c.campaign_id)
        assert last["event"] == "phase.started"


class TestCampaignStatusTransitions:
    def test_planning_to_active(self, tmp_path, make_app_finding):
        findings = [
            make_app_finding(
                finding_id="f-1", rule="ruff-b007", repo="test/repo", confidence=0.9
            )
        ]
        campaign = CampaignPlanner().plan(
            findings,
            repo="test/repo",
            title="Transition test",
        )
        assert campaign.status == CampaignStatus.planning

        campaign.status = CampaignStatus.active
        assert campaign.status == CampaignStatus.active

    def test_serialization_round_trip(self, tmp_path, make_app_finding):
        findings = [
            make_app_finding(
                finding_id="f-1", rule="ruff-b007", repo="test/repo", confidence=0.9
            ),
            make_app_finding(
                finding_id="f-2", rule="broad-except", repo="test/repo", confidence=0.9
            ),
        ]
        campaign = CampaignPlanner().plan(
            findings,
            repo="test/repo",
            title="Round trip",
        )

        data = campaign.to_dict()
        json_str = json.dumps(data)
        loaded_data = json.loads(json_str)
        restored = Campaign.from_dict(loaded_data)

        assert restored.campaign_id == campaign.campaign_id
        assert len(restored.phases) == len(campaign.phases)
        assert restored.total_findings == campaign.total_findings


# ────────────────────────────────────────────────────────────────────────
# Integrated executor lifecycle tests.
#
# These exercise CampaignExecutor.run() end-to-end against a real
# CampaignStateManager (tmp_path-backed) and a real git worktree. Only the
# per-finding fix_runner callable is mocked — the planner, state manager,
# lock, event log, rollback, and pause/resume machinery are all production
# code paths under test.
# ────────────────────────────────────────────────────────────────────────


def _make_findings(
    count: int = 5,
    rule_prefix: str = "S",
    repo: str = "test-repo",
    path_prefix: str = "src",
) -> List[Finding]:
    """Build `count` findings, each with a distinct rule (one phase per finding
    under the rule_based strategy)."""
    findings: List[Finding] = []
    for i in range(count):
        findings.append(
            Finding(
                finding_id=f"f-{rule_prefix.lower()}-{i:03d}",
                repo=repo,
                path=f"{path_prefix}/file_{i % 3}.py",
                line=10 + i,
                rule=f"{rule_prefix}{i:03d}",
                snippet=f"bad_code_{i}",
                confidence=0.8,
                quick_win=True,
                safe_to_autofix=True,
            )
        )
    return findings


def _make_grouped_findings(
    rules: List[str], per_rule: int = 2, repo: str = "test-repo"
) -> List[Finding]:
    """Build findings grouped by rule, each finding on its own path.

    Under the rule_based strategy each rule becomes one phase, so passing
    N rules yields N phases of `per_rule` findings each — convenient for
    multi-phase lifecycle assertions.
    """
    findings: List[Finding] = []
    for rule in rules:
        for i in range(per_rule):
            findings.append(
                Finding(
                    finding_id=f"f-{rule}-{i}",
                    repo=repo,
                    path=f"src/{rule}_{i}.py",
                    line=10 + i,
                    rule=rule,
                    snippet=f"bad_{rule}_{i}",
                    confidence=0.8,
                    quick_win=True,
                    safe_to_autofix=True,
                )
            )
    return findings


def _injectable_fix_runner(
    call_log: Optional[List[Dict[str, Any]]] = None,
    write_files: bool = False,
) -> Tuple[Callable[..., Dict[str, Any]], List[Dict[str, Any]]]:
    """Return a fix_runner that always succeeds and records every invocation.

    The runner matches the executor's contract:
        (campaign=, phase=, finding_id=, finding=, worktree_path=,
         pattern_store_path=) -> {"status": "success"|"failed", ...}

    When `write_files` is True the runner materialises the finding's path
    under the worktree, making "changes" observable on disk (used by the
    rollback test to prove phase-1 edits survive a later failure).
    """
    if call_log is None:
        call_log = []

    def runner(
        *,
        campaign: Any,
        phase: Any,
        finding_id: str,
        finding: Dict[str, Any],
        worktree_path: Path,
        pattern_store_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        call_log.append({"finding_id": finding_id, "phase_id": phase.phase_id})
        if write_files:
            rel = finding.get("path")
            if rel:
                target = Path(worktree_path) / str(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"fixed {finding_id}\n")
        return {"status": "success", "method": "injected"}

    return runner, call_log


def _failing_fix_runner(
    fail_on_finding_id: str, write_files: bool = False
) -> Callable[..., Dict[str, Any]]:
    """Return a fix_runner that fails for one finding id and succeeds otherwise.

    The failing finding returns before touching the filesystem, so its path
    is never created — this lets rollback assertions distinguish "rolled
    back" from "never written".
    """

    def runner(
        *,
        campaign: Any,
        phase: Any,
        finding_id: str,
        finding: Dict[str, Any],
        worktree_path: Path,
        pattern_store_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        if finding_id == fail_on_finding_id:
            return {"status": "failed", "error": "injected failure"}
        if write_files:
            rel = finding.get("path")
            if rel:
                target = Path(worktree_path) / str(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"fixed {finding_id}\n")
        return {"status": "success", "method": "injected"}

    return runner


@pytest.fixture
def campaigns_dir(tmp_path) -> Path:
    return tmp_path / "state" / "campaigns"


@pytest.fixture
def campaign_store(campaigns_dir) -> CampaignStateManager:
    return CampaignStateManager(campaigns_dir)


@pytest.fixture
def worktree(tmp_path) -> Path:
    """Plain worktree directory.

    The injected fix_runner never touches files for the happy-path / guard
    tests, and require_clean_worktree defaults to False, so a git repo is
    not required here. The rollback test uses the git_worktree fixture
    instead.
    """
    wt = tmp_path / "worktree"
    wt.mkdir()
    return wt


@pytest.fixture
def git_worktree(tmp_path) -> Path:
    """Minimal clean git repo for executor operations that require one."""
    repo = tmp_path / "git-worktree"
    repo.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "test@bluei.dev"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    (repo / "README").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


class TestCampaignExecutorLifecycle:
    """Integrated run/pause/resume/rollback lifecycle coverage for
    CampaignExecutor.run() — the half of the E2E plan that was previously
    missing."""

    def test_full_lifecycle_plan_save_dryrun_execute_pause_resume(
        self, campaign_store, campaigns_dir, worktree
    ):
        """Plan → save → dry-run → execute → pause mid-phase → resume → complete.

        Dry-run and live execution cannot share a campaign (a dry-run marks
        every phase completed), so a second campaign drives the live half.
        The live half fails mid-phase-0 to exercise the pause, then resumes
        to completion. Asserts on observable event-log contents and the
        status transitions — not internal executor state.
        """
        # ── Campaign A: dry-run path ──────────────────────────────────
        findings_a = _make_grouped_findings(["alpha", "beta"], per_rule=2)
        campaign_a = CampaignPlanner().plan(
            findings_a,
            repo="test-repo",
            title="Dry-run campaign",
            strategy=CampaignStrategy.rule_based,
        )
        assert len(campaign_a.phases) == 2
        campaign_store.save(campaign_a)
        assert (campaigns_dir / campaign_a.campaign_id / "campaign.json").exists()

        runner_a, call_log_a = _injectable_fix_runner()
        executor_a = CampaignExecutor(campaign_store, fix_runner=runner_a)
        dry = executor_a.run(
            campaign_a.campaign_id, dry_run=True, worktree_path=worktree
        )

        # Dry-run: everything skipped, fix_runner never invoked.
        assert dry["status"] == CampaignStatus.completed
        assert dry["findings_skipped"] == 4
        assert len(call_log_a) == 0

        dry_events = [e["event"] for e in campaign_store.events(campaign_a.campaign_id)]
        assert "campaign.started" in dry_events
        assert "finding.skipped" in dry_events
        assert "phase.completed" in dry_events
        assert "finding.fixed" not in dry_events

        # ── Campaign B: live execute → pause → resume → complete ──────
        findings_b = _make_grouped_findings(["alpha", "beta"], per_rule=2)
        campaign_b = CampaignPlanner().plan(
            findings_b,
            repo="test-repo",
            title="Live campaign",
            strategy=CampaignStrategy.rule_based,
        )
        assert len(campaign_b.phases) == 2
        campaign_store.save(campaign_b)

        # Succeed on phase 0's first finding, fail on its second → pause
        # mid-phase (one finding.fixed, then finding.failed).
        fail_id = campaign_b.phases[0].finding_ids[1]
        failing_executor = CampaignExecutor(
            campaign_store, fix_runner=_failing_fix_runner(fail_on_finding_id=fail_id)
        )
        paused = failing_executor.run(
            campaign_b.campaign_id, dry_run=False, worktree_path=worktree
        )
        assert paused["status"] == CampaignStatus.paused

        loaded_paused = campaign_store.load(campaign_b.campaign_id)
        assert loaded_paused.status == CampaignStatus.paused
        assert loaded_paused.pause_reason == "finding_failed"
        assert loaded_paused.pause_finding_id == fail_id
        assert loaded_paused.pause_phase_id == loaded_paused.phases[0].phase_id
        # Per-phase fixed counts are rolled back when the phase pauses.
        assert loaded_paused.phases[0].status == PhaseStatus.paused
        assert loaded_paused.phases[0].findings_fixed == 0

        # Resume with a succeeding runner — paused phase re-runs and the
        # campaign walks to completion.
        resume_runner, _ = _injectable_fix_runner()
        resume_executor = CampaignExecutor(campaign_store, fix_runner=resume_runner)
        done = resume_executor.run(
            campaign_b.campaign_id, dry_run=False, worktree_path=worktree
        )
        assert done["status"] == CampaignStatus.completed

        final = campaign_store.load(campaign_b.campaign_id)
        assert final.status == CampaignStatus.completed
        assert final.findings_fixed == 4
        assert all(p.status == PhaseStatus.completed for p in final.phases)
        assert final.pause_reason is None

        # Full event lifecycle is present in the log.
        events_b = [e["event"] for e in campaign_store.events(campaign_b.campaign_id)]
        assert "campaign.started" in events_b
        assert "phase.started" in events_b
        assert "finding.fixed" in events_b
        assert "finding.failed" in events_b
        assert "campaign.paused" in events_b
        assert "phase.completed" in events_b
        assert "campaign.completed" in events_b

    def test_multi_phase_with_rollback(self, campaign_store, git_worktree):
        """3-phase campaign → phase 1 fails → phase 0 changes preserved.

        With require_clean_worktree=True the executor snapshots each
        phase's files before mutating them and restores that snapshot when
        the phase pauses. Rollback is scoped to the *failing* phase only —
        phase 0's already-applied edits must survive on disk.
        """
        findings = _make_grouped_findings(["alpha", "beta", "gamma"], per_rule=2)
        campaign = CampaignPlanner().plan(
            findings,
            repo="test-repo",
            title="Rollback campaign",
            strategy=CampaignStrategy.rule_based,
        )
        assert len(campaign.phases) == 3
        campaign_store.save(campaign)

        # Phase 0 (alpha) fully succeeds; phase 1 (beta) trips on its first finding.
        fail_id = campaign.phases[1].finding_ids[0]
        executor = CampaignExecutor(
            campaign_store,
            fix_runner=_failing_fix_runner(
                fail_on_finding_id=fail_id, write_files=True
            ),
        )
        result = executor.run(
            campaign.campaign_id,
            dry_run=False,
            worktree_path=git_worktree,
            require_clean_worktree=True,
        )
        assert result["status"] == CampaignStatus.paused

        loaded = campaign_store.load(campaign.campaign_id)
        # Phase 0 completed with its fixes intact.
        assert loaded.phases[0].status == PhaseStatus.completed
        assert loaded.phases[0].findings_fixed == 2
        # Phase 1 paused at the injected failure; phase 2 untouched.
        assert loaded.phases[1].status == PhaseStatus.paused
        assert loaded.phases[2].status == PhaseStatus.pending
        assert loaded.pause_phase_id == loaded.phases[1].phase_id
        assert loaded.pause_finding_id == fail_id
        assert loaded.pause_reason == "finding_failed"

        # Observable on disk: phase 0's edits survive (NOT rolled back),
        # and the failing finding's file was never created.
        phase0_paths = [
            loaded.finding_snapshots[fid]["path"]
            for fid in loaded.phases[0].finding_ids
        ]
        for rel in phase0_paths:
            assert (git_worktree / rel).exists(), (
                f"phase 0 change at {rel} must persist after phase 1 failure"
            )
        fail_path = loaded.finding_snapshots[fail_id]["path"]
        assert not (git_worktree / fail_path).exists()

        # Observable in the event log: rollback was scoped to the failing
        # phase only — phase 0 never appears in a rollback event.
        events = campaign_store.events(campaign.campaign_id)
        rollback_events = [e for e in events if e["event"] == "phase.rollback.applied"]
        assert len(rollback_events) >= 1
        rolled_back_phases = {e["payload"]["phase_id"] for e in rollback_events}
        assert loaded.phases[1].phase_id in rolled_back_phases
        assert loaded.phases[0].phase_id not in rolled_back_phases

        # Resume with a succeeding runner. The worktree is now dirty from
        # phase 0's applied edits, so we drop the clean-worktree guard for
        # the resume (the rollback contract under test is already proven
        # above; resume just needs to walk to completion).
        resume_runner, _ = _injectable_fix_runner(write_files=True)
        resume_executor = CampaignExecutor(campaign_store, fix_runner=resume_runner)
        done = resume_executor.run(
            campaign.campaign_id, dry_run=False, worktree_path=git_worktree
        )
        assert done["status"] == CampaignStatus.completed

        final = campaign_store.load(campaign.campaign_id)
        assert final.status == CampaignStatus.completed
        assert final.findings_fixed == 6
        assert all(p.status == PhaseStatus.completed for p in final.phases)
        assert final.pause_reason is None

    def test_concurrent_execution_guard(self, campaign_store, campaigns_dir, worktree):
        """A second run while the campaign lock is held raises RuntimeError.

        CampaignExecutor guards concurrent execution with an exclusive lock
        file (O_CREAT | O_EXCL). Pre-creating the lock file simulates a run
        already in progress; the blocked run must fail before mutating any
        campaign state, and once the lock is released a subsequent run
        succeeds normally.
        """
        findings = _make_findings(count=3, rule_prefix="C")
        campaign = CampaignPlanner().plan(
            findings,
            repo="test-repo",
            title="Concurrent guard",
            strategy=CampaignStrategy.rule_based,
        )
        campaign_store.save(campaign)

        runner, _ = _injectable_fix_runner()

        # Simulate another executor already holding the lock.
        lock_path = campaigns_dir / campaign.campaign_id / "campaign.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
        assert lock_path.exists()

        blocked_executor = CampaignExecutor(campaign_store, fix_runner=runner)
        with pytest.raises(RuntimeError):
            blocked_executor.run(
                campaign.campaign_id, dry_run=False, worktree_path=worktree
            )

        # The blocked run must not have mutated any campaign state.
        assert campaign_store.events(campaign.campaign_id) == []
        unchanged = campaign_store.load(campaign.campaign_id)
        assert unchanged.status == CampaignStatus.planning
        assert unchanged.findings_fixed == 0

        # Release the lock (the simulated first run finishes) and retry.
        lock_path.unlink()

        final_executor = CampaignExecutor(campaign_store, fix_runner=runner)
        result = final_executor.run(
            campaign.campaign_id, dry_run=False, worktree_path=worktree
        )
        assert result["status"] == CampaignStatus.completed

        events = [e["event"] for e in campaign_store.events(campaign.campaign_id)]
        assert "campaign.started" in events
        assert "finding.fixed" in events
        assert "campaign.completed" in events
