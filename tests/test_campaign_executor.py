import json
import sys
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bluei.campaigns import executor as campaign_executor
from bluei.campaigns.executor import CampaignExecutor, autofix_fix_runner
from bluei.campaigns.planner import CampaignPlanner
from bluei.campaigns.state import CampaignStateManager
from bluei.campaigns.types import CampaignStatus, PhaseStatus
from bluei.engine.models import Finding


def test_campaign_executor_dry_run_walks_phases_and_records_events(tmp_path) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
            Finding(
                finding_id="f-2",
                repo="demo",
                path="src/api/orders.ts",
                line=3,
                rule="discount-math-sign",
                snippet="total - discount",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
        ],
        repo="demo",
        title="Dry-run campaign",
        target_rules=["discount-math-sign", "type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    result = CampaignExecutor(store).run(campaign.campaign_id, dry_run=True)

    assert result["status"] == "completed"
    assert result["dry_run"] is True
    assert result["phases_walked"] == 2
    assert result["findings_skipped"] == 2

    loaded = store.load(campaign.campaign_id)
    assert loaded.status == "completed"
    assert [phase.status for phase in loaded.phases] == ["completed", "completed"]
    assert loaded.current_phase_index == 1
    assert loaded.findings_skipped == 2

    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    event_names = [event["event"] for event in events]
    assert event_names == [
        "campaign.started",
        "phase.started",
        "finding.skipped",
        "phase.completed",
        "phase.started",
        "finding.skipped",
        "phase.completed",
        "campaign.completed",
    ]
    assert all(event["payload"].get("dry_run") is True for event in events)
    skipped = [event for event in events if event["event"] == "finding.skipped"]
    assert [event["payload"]["finding_id"] for event in skipped] == ["f-2", "f-1"]
    assert all(event["payload"]["reason"] == "dry_run" for event in skipped)


def test_campaign_executor_runs_validation_hooks_and_records_pass_events(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Validated dry-run campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    campaign.phases[0].pre_phase_checks = [[sys.executable, "-c", "print('pre ok')"]]
    campaign.phases[0].post_phase_checks = [[sys.executable, "-c", "print('post ok')"]]
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    result = CampaignExecutor(store).run(
        campaign.campaign_id,
        dry_run=True,
        worktree_path=tmp_path,
    )

    assert result["status"] == "completed"
    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    pass_events = [
        event for event in events if event["event"] == "phase.validation.passed"
    ]
    assert [event["payload"]["stage"] for event in pass_events] == ["pre", "post"]


def test_campaign_executor_pauses_on_failed_validation_and_releases_lock(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Failing dry-run campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    campaign.phases[0].pre_phase_checks = [
        [sys.executable, "-c", "raise SystemExit(7)"]
    ]
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    result = CampaignExecutor(store).run(
        campaign.campaign_id,
        dry_run=True,
        worktree_path=tmp_path,
    )

    assert result["status"] == "paused"
    assert result["phases_walked"] == 0
    assert not (tmp_path / campaign.campaign_id / "campaign.lock").exists()

    loaded = store.load(campaign.campaign_id)
    assert loaded.status == "paused"
    assert loaded.phases[0].status == "paused"

    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [event["event"] for event in events] == [
        "campaign.started",
        "phase.started",
        "phase.validation.failed",
        "campaign.paused",
    ]
    failure = events[2]["payload"]
    assert failure["stage"] == "pre"
    assert failure["returncode"] == 7


def test_campaign_executor_resume_skips_completed_phases(tmp_path) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
            Finding(
                finding_id="f-2",
                repo="demo",
                path="src/api/orders.ts",
                line=3,
                rule="discount-math-sign",
                snippet="total - discount",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
        ],
        repo="demo",
        title="Resume dry-run campaign",
        target_rules=["discount-math-sign", "type-explicit-any"],
        target_paths=["src/api/**"],
    )
    campaign.status = "paused"
    campaign.phases[0].status = "completed"
    campaign.findings_skipped = 1
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    result = CampaignExecutor(store).run(campaign.campaign_id, dry_run=True)

    assert result["status"] == "completed"
    assert result["phases_walked"] == 1
    assert result["findings_skipped"] == 1

    loaded = store.load(campaign.campaign_id)
    assert [phase.status for phase in loaded.phases] == ["completed", "completed"]
    assert loaded.findings_skipped == 2

    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    started_phase_ids = [
        event["payload"]["phase_id"]
        for event in events
        if event["event"] == "phase.started"
    ]
    assert started_phase_ids == [campaign.phases[1].phase_id]


def test_campaign_executor_applies_findings_with_injected_runner(tmp_path) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Apply campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path)
    store.save(campaign)
    calls: list[tuple[str, str, str]] = []

    def fix_runner(
        *,
        campaign: Any,
        phase: Any,
        finding_id: str,
        finding: dict[str, Any],
        worktree_path: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        calls.append((phase.phase_id, finding_id, finding["rule"]))
        return {"status": "success", "method": "unit-test"}

    result = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        worktree_path=tmp_path,
    )

    assert result["status"] == "completed"
    assert result["dry_run"] is False
    assert result["findings_fixed"] == 1
    assert calls == [(campaign.phases[0].phase_id, "f-1", "type-explicit-any")]

    loaded = store.load(campaign.campaign_id)
    assert loaded.findings_fixed == 1
    assert loaded.phases[0].findings_fixed == 1
    assert loaded.phases[0].status == "completed"

    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    fixed = [event for event in events if event["event"] == "finding.fixed"]
    assert fixed[0]["payload"]["finding_id"] == "f-1"
    assert fixed[0]["payload"]["method"] == "unit-test"
    assert all(event["payload"].get("dry_run") is False for event in events)


def test_campaign_executor_real_run_validation_events_record_mutation_mode(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Validated apply campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    campaign.phases[0].pre_phase_checks = [[sys.executable, "-c", "print('pre ok')"]]
    campaign.phases[0].post_phase_checks = [[sys.executable, "-c", "print('post ok')"]]
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    def fix_runner(**kwargs: Any) -> dict[str, Any]:
        return {"status": "success", "method": "unit-test"}

    result = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=tmp_path,
    )

    assert result["status"] == "completed"
    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    validation_events = [
        event
        for event in events
        if event["event"] in {"phase.validation.passed", "phase.validation.failed"}
    ]
    assert [event["payload"]["dry_run"] for event in validation_events] == [
        False,
        False,
    ]


def test_campaign_executor_requires_clean_git_worktree_for_guarded_real_run(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Guarded apply campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path / "state")
    store.save(campaign)
    worktree = tmp_path / "repo"
    worktree.mkdir()

    def fix_runner(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("fix runner should not run without a git worktree")

    try:
        CampaignExecutor(store, fix_runner=fix_runner).run(
            campaign.campaign_id,
            dry_run=False,
            worktree_path=worktree,
            require_clean_worktree=True,
        )
    except ValueError as exc:
        assert "git worktree" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    events = [
        json.loads(line)
        for line in (store.campaigns_dir / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [event["event"] for event in events] == ["campaign.preflight.failed"]
    assert events[0]["payload"]["reason"] == "clean_worktree_required"


def test_campaign_executor_pauses_on_injected_runner_failure(tmp_path) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Failing apply campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    def fix_runner(
        *,
        campaign: Any,
        phase: Any,
        finding_id: str,
        finding: dict[str, Any],
        worktree_path: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert finding["path"] == "src/api/users.ts"
        return {"status": "failed", "error": "no fix available"}

    result = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        worktree_path=tmp_path,
    )

    assert result["status"] == "paused"
    assert result["findings_failed"] == 1

    loaded = store.load(campaign.campaign_id)
    assert loaded.status == "paused"
    assert loaded.findings_failed == 1
    assert loaded.phases[0].findings_failed == 1
    assert loaded.phases[0].status == "paused"
    assert loaded.pause_reason == "finding_failed"
    assert loaded.pause_phase_id == campaign.phases[0].phase_id
    assert loaded.pause_finding_id == "f-1"

    events = [
        json.loads(line)
        for line in (tmp_path / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [event["event"] for event in events] == [
        "campaign.started",
        "phase.started",
        "finding.failed",
        "campaign.paused",
    ]
    assert events[2]["payload"]["error"] == "no fix available"


def test_campaign_executor_rolls_back_phase_files_when_later_finding_fails(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
            Finding(
                finding_id="f-2",
                repo="demo",
                path="src/api/users.ts",
                line=11,
                rule="type-explicit-any",
                snippet="let other: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
        ],
        repo="demo",
        title="Rollback apply campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path / "state")
    store.save(campaign)
    worktree = tmp_path / "repo"
    target = worktree / "src/api/users.ts"
    target.parent.mkdir(parents=True)
    target.write_text("let value: any\nlet other: any\n", encoding="utf-8")
    subprocess.run(
        ["git", "init"], cwd=worktree, text=True, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "bluei@example.invalid"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "bluei"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "src/api/users.ts"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )

    def fix_runner(
        *, finding_id: str, worktree_path: Any, **kwargs: Any
    ) -> dict[str, Any]:
        if finding_id == "f-1":
            target.write_text("let value: string\nlet other: any\n", encoding="utf-8")
            return {"status": "success", "method": "unit-test"}
        return {"status": "failed", "error": "second fix failed"}

    result = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert result["status"] == "paused"
    assert target.read_text(encoding="utf-8") == "let value: any\nlet other: any\n"
    loaded = store.load(campaign.campaign_id)
    assert loaded.findings_fixed == 0
    assert loaded.phases[0].findings_fixed == 0
    assert loaded.findings_failed == 1
    assert loaded.pause_reason == "finding_failed"
    assert loaded.pause_finding_id == "f-2"
    events = [
        json.loads(line)
        for line in (store.campaigns_dir / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    rollback = [event for event in events if event["event"] == "phase.rollback.applied"]
    assert rollback[0]["payload"]["reason"] == "finding_failed"
    assert rollback[0]["payload"]["paths"] == ["src/api/users.ts"]


def test_campaign_executor_rolls_back_phase_files_when_post_validation_fails(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Rollback validation campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    campaign.phases[0].post_phase_checks = [
        [sys.executable, "-c", "raise SystemExit(4)"]
    ]
    store = CampaignStateManager(tmp_path / "state")
    store.save(campaign)
    worktree = tmp_path / "repo"
    target = worktree / "src/api/users.ts"
    target.parent.mkdir(parents=True)
    target.write_text("let value: any\n", encoding="utf-8")
    subprocess.run(
        ["git", "init"], cwd=worktree, text=True, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "bluei@example.invalid"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "bluei"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "src/api/users.ts"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )

    def fix_runner(*, worktree_path: Any, **kwargs: Any) -> dict[str, Any]:
        target.write_text("let value: string\n", encoding="utf-8")
        return {"status": "success", "method": "unit-test"}

    result = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert result["status"] == "paused"
    assert target.read_text(encoding="utf-8") == "let value: any\n"
    loaded = store.load(campaign.campaign_id)
    assert loaded.findings_fixed == 0
    assert loaded.phases[0].findings_fixed == 0
    assert loaded.pause_reason == "validation_failed"
    assert loaded.pause_phase_id == campaign.phases[0].phase_id
    assert loaded.pause_finding_id is None
    events = [
        json.loads(line)
        for line in (store.campaigns_dir / campaign.campaign_id / "events.jsonl")
        .read_text()
        .splitlines()
    ]
    rollback = [event for event in events if event["event"] == "phase.rollback.applied"]
    assert rollback[0]["payload"]["reason"] == "validation_failed"
    assert rollback[0]["payload"]["paths"] == ["src/api/users.ts"]


def test_campaign_executor_can_resume_after_rollback_and_complete_phase(
    tmp_path,
) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
            Finding(
                finding_id="f-2",
                repo="demo",
                path="src/api/users.ts",
                line=11,
                rule="type-explicit-any",
                snippet="let other: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
        ],
        repo="demo",
        title="Resume rollback campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path / "state")
    store.save(campaign)
    worktree = tmp_path / "repo"
    target = worktree / "src/api/users.ts"
    target.parent.mkdir(parents=True)
    target.write_text("let value: any\nlet other: any\n", encoding="utf-8")
    subprocess.run(
        ["git", "init"], cwd=worktree, text=True, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "bluei@example.invalid"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "bluei"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "add", "src/api/users.ts"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=worktree,
        text=True,
        capture_output=True,
        check=True,
    )
    attempts: list[str] = []

    def fix_runner(
        *, finding_id: str, worktree_path: Any, **kwargs: Any
    ) -> dict[str, Any]:
        attempts.append(finding_id)
        if attempts == ["f-1"]:
            target.write_text("let value: string\nlet other: any\n", encoding="utf-8")
            return {"status": "success", "method": "unit-test"}
        if attempts == ["f-1", "f-2"]:
            return {"status": "failed", "error": "temporary failure"}
        if finding_id == "f-1":
            target.write_text("let value: string\nlet other: any\n", encoding="utf-8")
        else:
            target.write_text(
                "let value: string\nlet other: string\n", encoding="utf-8"
            )
        return {"status": "success", "method": "unit-test"}

    first = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )
    assert first["status"] == "paused"
    assert target.read_text(encoding="utf-8") == "let value: any\nlet other: any\n"

    loaded = store.load(campaign.campaign_id)
    loaded.status = "planning"
    loaded.phases[0].status = "pending"
    store.save(loaded)
    second = CampaignExecutor(store, fix_runner=fix_runner).run(
        campaign.campaign_id,
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert second["status"] == "completed"
    assert (
        target.read_text(encoding="utf-8") == "let value: string\nlet other: string\n"
    )
    reloaded = store.load(campaign.campaign_id)
    assert reloaded.pause_reason is None
    assert reloaded.phases[0].status == "completed"


def test_campaign_executor_requires_fix_runner_for_real_execution(tmp_path) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="No runner campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    try:
        CampaignExecutor(store).run(campaign.campaign_id, worktree_path=tmp_path)
    except ValueError as exc:
        assert "fix_runner" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_campaign_executor_refuses_to_run_aborted_campaign(tmp_path) -> None:
    campaign = CampaignPlanner().plan(
        [
            Finding(
                finding_id="f-1",
                repo="demo",
                path="src/api/users.ts",
                line=10,
                rule="type-explicit-any",
                snippet="let value: any",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            )
        ],
        repo="demo",
        title="Aborted campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/api/**"],
    )
    campaign.status = "aborted"
    campaign.abort_reason = "operator stopped it"
    store = CampaignStateManager(tmp_path)
    store.save(campaign)

    try:
        CampaignExecutor(store).run(
            campaign.campaign_id, dry_run=True, worktree_path=tmp_path
        )
    except ValueError as exc:
        assert "aborted" in str(exc)
    else:
        raise AssertionError("expected ValueError")

    loaded = store.load(campaign.campaign_id)
    assert loaded.status == "aborted"
    assert loaded.phases[0].status == "pending"


def test_autofix_fix_runner_adapts_finding_snapshot_to_sandbox_autofix(
    tmp_path, monkeypatch
) -> None:
    calls: list[tuple[Any, Any, Any]] = []

    def fake_apply_autofix(worktree_path: Any, finding: Any, log_file: Any) -> bool:
        calls.append((worktree_path, finding, log_file))
        return True

    monkeypatch.setattr(campaign_executor, "apply_autofix", fake_apply_autofix)

    result = autofix_fix_runner(
        campaign=type("Campaign", (), {"campaign_id": "camp-test"})(),
        phase=type("Phase", (), {"phase_id": "phase-test"})(),
        finding_id="f-1",
        finding={
            "finding_id": "f-1",
            "repo": "demo",
            "path": "src/app.py",
            "line": 12,
            "rule": "trailing-whitespace",
            "snippet": "x ",
            "confidence": 0.9,
            "quick_win": True,
            "safe_to_autofix": True,
        },
        worktree_path=tmp_path,
    )

    assert result["status"] == "success"
    assert result["method"] == "autofix"
    # Additive Campaign Lab keys (alpha.5) — present but do not change the
    # status/method/error contract consumed by existing readers.
    assert result["deterministic"] is True
    assert result["matched_asset"] is None
    assert result["cascade_stage"] is None
    assert len(calls) == 1
    worktree_path, adapted_finding, log_file = calls[0]
    assert worktree_path == tmp_path
    assert adapted_finding.finding_id == "f-1"
    assert adapted_finding.rule == "trailing-whitespace"
    assert adapted_finding.path == "src/app.py"
    assert log_file == tmp_path / ".bluei" / "campaign-autofix.log"


def test_autofix_fix_runner_reports_missing_snapshot_fields(tmp_path) -> None:
    result = autofix_fix_runner(
        campaign=type("Campaign", (), {"campaign_id": "camp-test"})(),
        phase=type("Phase", (), {"phase_id": "phase-test"})(),
        finding_id="f-1",
        finding={"finding_id": "f-1", "rule": "trailing-whitespace"},
        worktree_path=tmp_path,
    )

    assert result["status"] == "failed"
    assert "missing finding snapshot field" in result["error"]


# ── Tests extracted from test_campaigns_scaffold_tiers_remaining.py ─────────


def _valid_snapshot(**overrides):
    defaults = dict(
        finding_id="f1",
        repo="repo",
        path="src/app.py",
        line=10,
        rule="ruff-e501",
        snippet="x=1",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
        fix_attempts=0,
        last_fix_error=None,
        last_fix_at=None,
        fix_success=False,
    )
    defaults.update(overrides)
    return defaults


def _make_campaign_data(tmp_path, findings_count=1, phases_status=None):
    store = CampaignStateManager(tmp_path / "campaigns")
    planner = CampaignPlanner()
    findings = [
        _finding(
            finding_id=f"f{i}",
            path=f"src/f{i}.py",
            line=i,
            rule="lint-rule",
        )
        for i in range(findings_count)
    ]
    campaign = planner.plan(findings, repo="repo", title="test")
    if phases_status is not None:
        for phase in campaign.phases:
            phase.status = phases_status
    store.save(campaign)
    return store, campaign


def _finding(**overrides):
    defaults = dict(
        finding_id="f1",
        repo="repo",
        path="src/app.py",
        line=10,
        rule="ruff-e501",
        snippet="x=1",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )
    defaults.update(overrides)
    return Finding(**defaults)


class TestAutofixFixRunnerSnapshotErrors:
    def test_missing_required_field_returns_failed(self, tmp_path):
        snapshot = {"path": "x.py"}
        result = autofix_fix_runner(
            campaign=MagicMock(),
            phase=MagicMock(),
            finding_id="f1",
            finding=snapshot,
            worktree_path=tmp_path,
        )
        assert result["status"] == "failed"
        assert "missing finding snapshot field" in result["error"]

    def test_type_error_in_snapshot(self, tmp_path):
        snapshot = _valid_snapshot(line="not_a_number")
        result = autofix_fix_runner(
            campaign=MagicMock(),
            phase=MagicMock(),
            finding_id="f1",
            finding=snapshot,
            worktree_path=tmp_path,
        )
        assert result["status"] == "failed"

    def test_value_error_in_snapshot(self, tmp_path):
        snapshot = _valid_snapshot(confidence="bad")
        result = autofix_fix_runner(
            campaign=MagicMock(),
            phase=MagicMock(),
            finding_id="f1",
            finding=snapshot,
            worktree_path=tmp_path,
        )
        assert result["status"] == "failed"


class TestAutofixFixRunnerPatternReplay:
    def test_pattern_replay_success(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("old_code()\n")

        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_pattern = MagicMock()
        mock_pattern.confidence = 0.95
        mock_pattern.before_snippet = "old_code()"
        mock_pattern.after_snippet = "new_code()"
        mock_store.lookup.return_value = mock_pattern

        snapshot = _valid_snapshot(path="src/app.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            result = autofix_fix_runner(
                campaign=MagicMock(),
                phase=MagicMock(),
                finding_id="f1",
                finding=snapshot,
                worktree_path=tmp_path,
                pattern_store_path=store_path,
            )
        assert result["status"] == "success"
        assert result["method"] == "pattern_replay"
        assert src_file.read_text() == "new_code()\n"

    def test_pattern_replay_skipped_when_confidence_low(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("old_code()\n")

        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_pattern = MagicMock()
        mock_pattern.confidence = 0.5
        mock_pattern.before_snippet = "old_code()"
        mock_pattern.after_snippet = "new_code()"
        mock_store.lookup.return_value = mock_pattern

        snapshot = _valid_snapshot(path="src/app.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            with patch.object(campaign_executor, "apply_autofix", return_value=True):
                result = autofix_fix_runner(
                    campaign=MagicMock(),
                    phase=MagicMock(),
                    finding_id="f1",
                    finding=snapshot,
                    worktree_path=tmp_path,
                    pattern_store_path=store_path,
                )
        assert result["status"] == "success"
        assert result["method"] == "autofix"

    def test_pattern_replay_skipped_when_before_not_in_content(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("totally_different()\n")

        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_pattern = MagicMock()
        mock_pattern.confidence = 0.95
        mock_pattern.before_snippet = "old_code()"
        mock_pattern.after_snippet = "new_code()"
        mock_store.lookup.return_value = mock_pattern

        snapshot = _valid_snapshot(path="src/app.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            with patch.object(campaign_executor, "apply_autofix", return_value=True):
                result = autofix_fix_runner(
                    campaign=MagicMock(),
                    phase=MagicMock(),
                    finding_id="f1",
                    finding=snapshot,
                    worktree_path=tmp_path,
                    pattern_store_path=store_path,
                )
        assert result["method"] == "autofix"

    def test_pattern_replay_skipped_when_target_file_missing(self, tmp_path):
        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_pattern = MagicMock()
        mock_pattern.confidence = 0.95
        mock_pattern.before_snippet = "old_code()"
        mock_pattern.after_snippet = "new_code()"
        mock_store.lookup.return_value = mock_pattern

        snapshot = _valid_snapshot(path="src/missing.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            with patch.object(campaign_executor, "apply_autofix", return_value=True):
                result = autofix_fix_runner(
                    campaign=MagicMock(),
                    phase=MagicMock(),
                    finding_id="f1",
                    finding=snapshot,
                    worktree_path=tmp_path,
                    pattern_store_path=store_path,
                )
        assert result["method"] == "autofix"

    def test_pattern_replay_skipped_when_before_snippet_empty(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("code()\n")

        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_pattern = MagicMock()
        mock_pattern.confidence = 0.95
        mock_pattern.before_snippet = ""
        mock_pattern.after_snippet = "new_code()"
        mock_store.lookup.return_value = mock_pattern

        snapshot = _valid_snapshot(path="src/app.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            with patch.object(campaign_executor, "apply_autofix", return_value=True):
                result = autofix_fix_runner(
                    campaign=MagicMock(),
                    phase=MagicMock(),
                    finding_id="f1",
                    finding=snapshot,
                    worktree_path=tmp_path,
                    pattern_store_path=store_path,
                )
        assert result["method"] == "autofix"

    def test_pattern_lookup_returns_none(self, tmp_path):
        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_store.lookup.return_value = None

        snapshot = _valid_snapshot(path="src/app.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            with patch.object(campaign_executor, "apply_autofix", return_value=True):
                result = autofix_fix_runner(
                    campaign=MagicMock(),
                    phase=MagicMock(),
                    finding_id="f1",
                    finding=snapshot,
                    worktree_path=tmp_path,
                    pattern_store_path=store_path,
                )
        assert result["method"] == "autofix"

    def test_pattern_store_exception_falls_through(self, tmp_path):
        store_path = tmp_path / "fake_store.jsonl"
        store_path.write_text("{}\n")

        mock_store = MagicMock()
        mock_store.lookup.side_effect = Exception("boom")

        snapshot = _valid_snapshot(path="src/app.py")

        with patch.object(
            campaign_executor, "FixPatternStore", return_value=mock_store
        ):
            with patch.object(campaign_executor, "apply_autofix", return_value=True):
                result = autofix_fix_runner(
                    campaign=MagicMock(),
                    phase=MagicMock(),
                    finding_id="f1",
                    finding=snapshot,
                    worktree_path=tmp_path,
                    pattern_store_path=store_path,
                )
        assert result["status"] == "success"
        assert result["method"] == "autofix"

    def test_autofix_returns_false(self, tmp_path):
        snapshot = _valid_snapshot()
        with patch("bluei.campaigns.executor.apply_autofix", return_value=False):
            result = autofix_fix_runner(
                campaign=MagicMock(),
                phase=MagicMock(),
                finding_id="f1",
                finding=snapshot,
                worktree_path=tmp_path,
            )
        assert result["status"] == "failed"
        assert result["method"] == "autofix"

    def test_autofix_raises_exception(self, tmp_path):
        snapshot = _valid_snapshot()
        with patch(
            "bluei.campaigns.executor.apply_autofix", side_effect=RuntimeError("boom")
        ):
            result = autofix_fix_runner(
                campaign=MagicMock(),
                phase=MagicMock(),
                finding_id="f1",
                finding=snapshot,
                worktree_path=tmp_path,
            )
        assert result["status"] == "failed"
        assert "RuntimeError" in result["error"]


class TestCampaignExecutorRun:
    def test_run_without_fix_runner_non_dry_raises(self, tmp_path):
        store = CampaignStateManager(tmp_path / "campaigns")
        exe = CampaignExecutor(store)
        with pytest.raises(ValueError, match="fix_runner"):
            exe.run("camp-x", dry_run=False)

    def test_run_aborted_campaign_raises(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        campaign.status = CampaignStatus.aborted
        store.save(campaign)
        exe = CampaignExecutor(store, fix_runner=MagicMock())
        with pytest.raises(ValueError, match="aborted"):
            exe.run(campaign.campaign_id, dry_run=True)

    def test_run_dry_run_skips_findings(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path, findings_count=2)
        exe = CampaignExecutor(store)
        result = exe.run(campaign.campaign_id, dry_run=True)
        assert result["dry_run"] is True
        assert result["findings_skipped"] >= 0
        assert result["status"] == CampaignStatus.completed

    def test_run_live_with_successful_fixes(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path, findings_count=1)
        fix_runner = MagicMock(return_value={"status": "success", "method": "autofix"})
        exe = CampaignExecutor(store, fix_runner=fix_runner)
        result = exe.run(campaign.campaign_id, dry_run=False)
        assert result["status"] == CampaignStatus.completed
        assert result["findings_fixed"] >= 1

    def test_run_live_fix_failure_pauses(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path, findings_count=2)
        fix_runner = MagicMock(return_value={"status": "failed", "error": "bad"})
        exe = CampaignExecutor(store, fix_runner=fix_runner)
        result = exe.run(campaign.campaign_id, dry_run=False)
        assert result["status"] == CampaignStatus.paused
        assert result["findings_failed"] >= 1

    def test_require_clean_worktree_dirty_raises(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store, fix_runner=MagicMock())
        with patch.object(
            exe, "_require_clean_git_worktree", side_effect=ValueError("dirty")
        ):
            with pytest.raises(ValueError, match="dirty"):
                exe.run(
                    campaign.campaign_id,
                    dry_run=False,
                    worktree_path=tmp_path,
                    require_clean_worktree=True,
                )

    def test_lock_conflict_raises_runtime_error(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        lock_path = store.campaigns_dir / campaign.campaign_id / "campaign.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("locked")
        exe = CampaignExecutor(store, fix_runner=MagicMock())
        with pytest.raises(RuntimeError, match="already running"):
            exe.run(campaign.campaign_id, dry_run=True)

    def test_lock_released_after_run(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        exe.run(campaign.campaign_id, dry_run=True)
        lock_path = store.campaigns_dir / campaign.campaign_id / "campaign.lock"
        assert not lock_path.exists()

    def test_lock_cleanup_handles_file_not_found(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        exe.run(campaign.campaign_id, dry_run=True)
        lock_path = store.campaigns_dir / campaign.campaign_id / "campaign.lock"
        lock_path.unlink(missing_ok=True)
        assert not lock_path.exists()

    def test_completed_phases_skipped(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path, findings_count=1)
        for phase in campaign.phases:
            phase.status = PhaseStatus.completed
        store.save(campaign)
        exe = CampaignExecutor(store, fix_runner=MagicMock())
        result = exe.run(campaign.campaign_id, dry_run=False)
        assert result["status"] == CampaignStatus.completed
        assert result["phases_walked"] == 0


class TestCampaignExecutorValidation:
    def test_pre_phase_check_failure_pauses(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path, findings_count=1)
        for phase in campaign.phases:
            phase.pre_phase_checks = [["false"]]
        store.save(campaign)
        fix_runner = MagicMock(return_value={"status": "success", "method": "autofix"})
        exe = CampaignExecutor(store, fix_runner=fix_runner)
        result = exe.run(campaign.campaign_id, dry_run=False)
        assert result["status"] == CampaignStatus.paused

    def test_post_phase_check_failure_pauses(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path, findings_count=1)
        for phase in campaign.phases:
            phase.post_phase_checks = [["false"]]
        store.save(campaign)
        fix_runner = MagicMock(return_value={"status": "success", "method": "autofix"})
        exe = CampaignExecutor(store, fix_runner=fix_runner)
        result = exe.run(campaign.campaign_id, dry_run=False)
        assert result["status"] == CampaignStatus.paused

    def test_validation_checks_pass_returns_none(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store, fix_runner=MagicMock())
        for phase in campaign.phases:
            phase.pre_phase_checks = [["true"]]
        store.save(campaign)
        result = exe.run(campaign.campaign_id, dry_run=True)
        assert result["status"] == CampaignStatus.completed

    def test_validation_stdout_captured(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        for phase in campaign.phases:
            phase.pre_phase_checks = [["echo", "hello world"]]
        store.save(campaign)
        exe = CampaignExecutor(store)
        result = exe.run(campaign.campaign_id, dry_run=True)
        assert result["status"] == CampaignStatus.completed


class TestCampaignExecutorSnapshotRollback:
    def test_snapshot_phase_files_reads_content(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("original", encoding="utf-8")

        store, campaign = _make_campaign_data(tmp_path, findings_count=1)
        for phase in campaign.phases:
            phase.finding_ids = ["f0"]
        campaign.finding_snapshots = {"f0": {"path": "src/app.py"}}

        exe = CampaignExecutor(store)
        phase = campaign.phases[0]
        snapshots = exe._snapshot_phase_files(campaign, phase, tmp_path)
        assert "src/app.py" in snapshots
        assert snapshots["src/app.py"] == b"original"

    def test_snapshot_skips_missing_path(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        for phase in campaign.phases:
            phase.finding_ids = ["f0"]
        campaign.finding_snapshots = {"f0": {"path": "nonexistent.py"}}
        exe = CampaignExecutor(store)
        snapshots = exe._snapshot_phase_files(campaign, campaign.phases[0], tmp_path)
        assert snapshots.get("nonexistent.py") is None

    def test_snapshot_skips_duplicate_paths(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("content", encoding="utf-8")

        store, campaign = _make_campaign_data(tmp_path)
        for phase in campaign.phases:
            phase.finding_ids = ["f0", "f1"]
        campaign.finding_snapshots = {
            "f0": {"path": "src/app.py"},
            "f1": {"path": "src/app.py"},
        }
        exe = CampaignExecutor(store)
        snapshots = exe._snapshot_phase_files(campaign, campaign.phases[0], tmp_path)
        assert len(snapshots) == 1

    def test_rollback_restores_files(self, tmp_path):
        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("modified", encoding="utf-8")

        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        exe._rollback_phase_files(
            campaign.campaign_id,
            "phase-0",
            tmp_path,
            {"src/app.py": b"original"},
            reason="test",
        )
        assert src_file.read_text() == "original"

    def test_rollback_deletes_new_files(self, tmp_path):
        new_file = tmp_path / "new_file.py"
        new_file.write_text("should be removed", encoding="utf-8")

        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        exe._rollback_phase_files(
            campaign.campaign_id,
            "phase-0",
            tmp_path,
            {"new_file.py": None},
            reason="test",
        )
        assert not new_file.exists()

    def test_rollback_empty_snapshots_noop(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        exe._rollback_phase_files(
            campaign.campaign_id, "phase-0", tmp_path, {}, reason="test"
        )

    def test_rollback_creates_parent_dirs(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        exe._rollback_phase_files(
            campaign.campaign_id,
            "phase-0",
            tmp_path,
            {"deep/nested/file.py": b"content"},
            reason="test",
        )
        assert (tmp_path / "deep" / "nested" / "file.py").read_text() == "content"


class TestCampaignExecutorWorktree:
    def test_require_clean_git_worktree_nonexistent_dir(self, tmp_path):
        exe = CampaignExecutor(MagicMock())
        with pytest.raises(ValueError, match="clean git worktree"):
            exe._require_clean_git_worktree(tmp_path / "nonexistent")

    def test_require_clean_git_worktree_not_a_git_repo(self, tmp_path):
        exe = CampaignExecutor(MagicMock())
        with pytest.raises(ValueError, match="clean git worktree"):
            exe._require_clean_git_worktree(tmp_path)

    def test_require_clean_git_worktree_dirty(self, tmp_path):
        exe = CampaignExecutor(MagicMock())
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="true\n"),
                MagicMock(returncode=0, stdout="M file.py\n"),
            ]
            with pytest.raises(ValueError, match="clean git worktree"):
                exe._require_clean_git_worktree(tmp_path)

    def test_require_clean_git_worktree_git_status_fails(self, tmp_path):
        exe = CampaignExecutor(MagicMock())
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="true\n"),
                MagicMock(returncode=128, stdout=""),
            ]
            with pytest.raises(ValueError, match="clean git worktree"):
                exe._require_clean_git_worktree(tmp_path)

    def test_apply_finding_no_runner_raises(self, tmp_path):
        store, campaign = _make_campaign_data(tmp_path)
        exe = CampaignExecutor(store)
        with pytest.raises(ValueError, match="fix_runner"):
            exe._apply_finding(
                campaign=campaign,
                phase=campaign.phases[0] if campaign.phases else MagicMock(),
                finding_id="f1",
                finding={},
                worktree_path=tmp_path,
            )
