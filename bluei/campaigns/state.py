"""Persistent JSON-backed state manager for campaigns.

Reads and writes campaign metadata, per-phase data, and an append-only
event log under a configurable campaigns directory.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any, Dict

from .planner import Campaign
from bluei.app.models import now_iso
from bluei.app.state import _atomic_json_write

_logger = logging.getLogger(__name__)


class CampaignStateManager:
    """Store campaign metadata under state/campaigns/{campaign_id}/."""

    def __init__(self, campaigns_dir: Path):
        """
        Args:
            campaigns_dir: Root directory for all campaign data (e.g. state/campaigns/).
        """
        self.campaigns_dir = Path(campaigns_dir)

    def save(self, campaign: Campaign) -> None:
        """Persist campaign metadata, dependency graph, and per-phase data to disk.

        Args:
            campaign: Campaign object to serialize.

        Returns:
            None
        """
        campaign_dir = self._campaign_dir(campaign.campaign_id)
        phases_dir = campaign_dir / "phases"
        phases_dir.mkdir(parents=True, exist_ok=True)

        _atomic_json_write(campaign_dir / "campaign.json", campaign.to_dict())
        _atomic_json_write(campaign_dir / "dependency_graph.json", campaign.dependency_graph)
        for phase in campaign.phases:
            _atomic_json_write(phases_dir / f"{phase.phase_id}.json", phase.to_dict())

    def load(self, campaign_id: str) -> Campaign:
        """Load a campaign from its persisted JSON file.

        Args:
            campaign_id: Unique campaign identifier.

        Returns:
            Reconstructed Campaign object.
        """
        campaign_file = self._campaign_dir(campaign_id) / "campaign.json"
        with campaign_file.open(encoding="utf-8") as handle:
            return Campaign.from_dict(json.load(handle))

    def list(self) -> list[Campaign]:
        """Return all persisted campaigns sorted by creation time (newest first).

        Returns:
            List of Campaign objects; empty list if directory does not exist.
        """
        campaigns: list[Campaign] = []
        if not self.campaigns_dir.exists():
            return campaigns
        for campaign_dir in self.campaigns_dir.iterdir():
            campaign_file = campaign_dir / "campaign.json"
            if not campaign_file.exists():
                continue
            try:
                with campaign_file.open(encoding="utf-8") as handle:
                    campaigns.append(Campaign.from_dict(json.load(handle)))
            except Exception:
                continue
        campaigns.sort(key=lambda campaign: campaign.created_at or "", reverse=True)
        return campaigns

    def append_event(
        self,
        campaign_id: str,
        event: str,
        payload: Dict[str, Any] | None = None,
    ) -> None:
        """Append a timestamped event record to the campaign's JSONL event log.

        Args:
            campaign_id: Campaign to log the event under.
            event: Dot-namespaced event type (e.g. "phase.started").
            payload: Optional structured data attached to the event.

        Returns:
            None
        """
        campaign_dir = self._campaign_dir(campaign_id)
        campaign_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": now_iso(),
            "event": event,
            "payload": payload or {},
        }
        with (campaign_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def last_event(self, campaign_id: str) -> Dict[str, Any] | None:
        """Return the most recent event for a campaign, or None if no events exist.

        Args:
            campaign_id: Campaign to query.

        Returns:
            Latest event dict, or None.
        """
        events = self.events(campaign_id, limit=1)
        if not events:
            return None
        return events[-1]

    def events(self, campaign_id: str, limit: int | None = None) -> list[Dict[str, Any]]:
        """Read the full event log for a campaign, optionally limited to the last N entries.

        Args:
            campaign_id: Campaign to query.
            limit: If set, return only the last `limit` events.

        Returns:
            List of event dicts ordered chronologically.
        """
        events_file = self._campaign_dir(campaign_id) / "events.jsonl"
        if not events_file.exists():
            return []
        records: list[Dict[str, Any]] = []
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if limit is not None and limit > 0:
            return records[-limit:]
        return records

    def _campaign_dir(self, campaign_id: str) -> Path:
        return self.campaigns_dir / campaign_id
