import json

from bluei.campaigns.planner import CampaignPlanner
from bluei.campaigns.state import CampaignStateManager
from bluei.engine.models import Finding


def test_campaign_state_persists_campaign_and_event_log(tmp_path) -> None:
    finding = Finding(
        finding_id="f-1",
        repo="demo",
        path="src/app.ts",
        line=4,
        rule="type-explicit-any",
        snippet="let value: any",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )
    campaign = CampaignPlanner().plan(
        [finding],
        repo="demo",
        title="Fix type findings",
        target_rules=["type-explicit-any"],
        target_paths=["src/**"],
    )

    manager = CampaignStateManager(tmp_path)
    manager.save(campaign)
    manager.append_event(campaign.campaign_id, "campaign.created", {"repo": "demo"})

    campaign_file = tmp_path / campaign.campaign_id / "campaign.json"
    graph_file = tmp_path / campaign.campaign_id / "dependency_graph.json"
    events_file = tmp_path / campaign.campaign_id / "events.jsonl"

    assert campaign_file.exists()
    assert graph_file.exists()
    assert events_file.exists()

    loaded = manager.load(campaign.campaign_id)
    assert loaded.campaign_id == campaign.campaign_id
    assert loaded.phases[0].finding_ids == ["f-1"]

    event = json.loads(events_file.read_text().strip())
    assert event["event"] == "campaign.created"
    assert event["payload"] == {"repo": "demo"}
    assert manager.last_event(campaign.campaign_id) == event
    manager.append_event(campaign.campaign_id, "campaign.started", {"dry_run": True})
    assert [
        record["event"] for record in manager.events(campaign.campaign_id, limit=1)
    ] == ["campaign.started"]
    assert [record["event"] for record in manager.events(campaign.campaign_id)] == [
        "campaign.created",
        "campaign.started",
    ]
    assert manager.last_event("missing") is None
    assert manager.events("missing") == []


def test_campaign_state_lists_campaigns_newest_first(tmp_path) -> None:
    manager = CampaignStateManager(tmp_path)
    campaigns = []
    for title in ("First", "Second"):
        campaign = CampaignPlanner().plan(
            [
                Finding(
                    finding_id=f"f-{title}",
                    repo="demo",
                    path="src/app.ts",
                    line=4,
                    rule="type-explicit-any",
                    snippet="let value: any",
                    confidence=0.9,
                    quick_win=True,
                    safe_to_autofix=True,
                )
            ],
            repo="demo",
            title=title,
            target_rules=["type-explicit-any"],
            target_paths=["src/**"],
        )
        manager.save(campaign)
        campaigns.append(campaign)

    listed = manager.list()

    assert [campaign.campaign_id for campaign in listed] == [
        campaigns[1].campaign_id,
        campaigns[0].campaign_id,
    ]


# ── Extracted from test_small_tier23_remaining.py ──
# Additional campaign state edge cases


def _make_campaign(tmp_path, campaign_id="camp-test-001"):
    finding = Finding(
        finding_id="f-1",
        repo="demo",
        path="src/app.ts",
        line=4,
        rule="type-explicit-any",
        snippet="let value: any",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )
    return CampaignPlanner().plan(
        [finding],
        repo="demo",
        title="Test campaign",
        target_rules=["type-explicit-any"],
        target_paths=["src/**"],
    )


class TestCampaignStateListNonexistentDir:
    def test_list_returns_empty_when_dir_missing(self, tmp_path):
        manager = CampaignStateManager(tmp_path / "nonexistent")
        result = manager.list()
        assert result == []


class TestCampaignStateListMissingCampaignJson:
    def test_list_skips_dir_without_campaign_json(self, tmp_path):
        camp_dir = tmp_path / "camp-no-json"
        camp_dir.mkdir()
        (camp_dir / "other_file.txt").write_text("hello")
        manager = CampaignStateManager(tmp_path)
        result = manager.list()
        assert result == []


class TestCampaignStateListCorruptCampaignJson:
    def test_list_skips_corrupt_campaign_json(self, tmp_path):
        camp_dir = tmp_path / "camp-corrupt"
        camp_dir.mkdir()
        (camp_dir / "campaign.json").write_text("{invalid json")
        manager = CampaignStateManager(tmp_path)
        result = manager.list()
        assert result == []


class TestCampaignStateEventsEmptyLines:
    def test_events_skips_empty_lines(self, tmp_path):
        manager = CampaignStateManager(tmp_path)
        manager.append_event("camp-1", "phase.started", {"step": 1})
        events_file = tmp_path / "camp-1" / "events.jsonl"
        original = events_file.read_text()
        events_file.write_text("\n\n" + original + "\n   \n")
        events = manager.events("camp-1")
        assert len(events) == 1
        assert events[0]["event"] == "phase.started"


class TestCampaignStateEventsCorruptJson:
    def test_events_skips_corrupt_json_lines(self, tmp_path):
        manager = CampaignStateManager(tmp_path)
        manager.append_event("camp-1", "phase.started")
        events_file = tmp_path / "camp-1" / "events.jsonl"
        original = events_file.read_text()
        events_file.write_text("{broken}\n" + original)
        events = manager.events("camp-1")
        assert len(events) == 1
        assert events[0]["event"] == "phase.started"
