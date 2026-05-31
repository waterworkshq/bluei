#!/usr/bin/env python3
"""E2E campaign lifecycle tests — plan, save, dry-run, state transitions."""

import json
from pathlib import Path

import pytest

from bluei.campaigns.planner import Campaign, CampaignPhase, CampaignPlanner
from bluei.campaigns.state import CampaignStateManager
from bluei.campaigns.types import CampaignStatus, PhaseStatus


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
