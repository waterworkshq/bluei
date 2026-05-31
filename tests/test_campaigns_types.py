"""Tests for bluei.campaigns.types — enums used in campaign orchestration."""

import json

from bluei.campaigns.types import (
    CampaignStatus,
    CampaignStrategy,
    PhaseStatus,
    PhaseExecutionMode,
)


class TestCampaignStatus:
    def test_all_values_are_strings_serializable(self):
        for member in CampaignStatus:
            json.dumps(member.value)

    def test_unique_values(self):
        vals = [m.value for m in CampaignStatus]
        assert len(vals) == len(set(vals))

    def test_default_is_planning(self):
        assert CampaignStatus.planning.value == "planning"

    def test_terminal_statuses(self):
        terminal = {CampaignStatus.completed, CampaignStatus.aborted}
        for s in terminal:
            assert isinstance(s, CampaignStatus)

    def test_from_string(self):
        assert CampaignStatus("active") == CampaignStatus.active
        assert CampaignStatus("paused") == CampaignStatus.paused


class TestCampaignStrategy:
    def test_all_values_are_strings(self):
        for member in CampaignStrategy:
            assert isinstance(member.value, str)

    def test_unique_values(self):
        vals = [m.value for m in CampaignStrategy]
        assert len(vals) == len(set(vals))

    def test_from_string(self):
        assert CampaignStrategy("rule_based") == CampaignStrategy.rule_based
        assert CampaignStrategy("depth_first") == CampaignStrategy.depth_first
        assert (
            CampaignStrategy("dependency_ordered")
            == CampaignStrategy.dependency_ordered
        )


class TestPhaseStatus:
    def test_all_values_are_strings(self):
        for member in PhaseStatus:
            assert isinstance(member.value, str)

    def test_unique_values(self):
        vals = [m.value for m in PhaseStatus]
        assert len(vals) == len(set(vals))

    def test_from_string(self):
        assert PhaseStatus("pending") == PhaseStatus.pending
        assert PhaseStatus("active") == PhaseStatus.active
        assert PhaseStatus("completed") == PhaseStatus.completed
        assert PhaseStatus("paused") == PhaseStatus.paused


class TestPhaseExecutionMode:
    def test_all_values_are_strings(self):
        for member in PhaseExecutionMode:
            assert isinstance(member.value, str)

    def test_unique_values(self):
        vals = [m.value for m in PhaseExecutionMode]
        assert len(vals) == len(set(vals))

    def test_from_string(self):
        assert PhaseExecutionMode("sequential") == PhaseExecutionMode.sequential
        assert PhaseExecutionMode("parallel") == PhaseExecutionMode.parallel
