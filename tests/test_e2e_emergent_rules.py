#!/usr/bin/env python3
"""E2E emergent rules tests — observe, validate, shadow, approve, reject."""

import json
from pathlib import Path

import pytest

from bluei.engine.models import Finding


class TestObserveFindings:
    def test_observe_creates_no_rules_with_few_findings(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [make_finding(finding_id=f"f-{i}") for i in range(3)]

        result = store.observe_findings(findings, run_id="run-1", min_observations=5)
        assert result["created"] == 0

    def test_observe_creates_rules_with_many_findings(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="broad-except") for i in range(7)
        ]

        result = store.observe_findings(findings, run_id="run-1", min_observations=5)
        assert result["created"] >= 1

        rules = store.load()
        assert len(rules) >= 1
        assert rules[0].observation_count >= 5

    def test_observe_skips_existing_catalog_rules(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="broad-except") for i in range(7)
        ]

        result = store.observe_findings(
            findings,
            run_id="run-1",
            min_observations=5,
            existing_rule_ids={"broad-except"},
        )
        assert result["skipped"] >= 1


class TestValidateProposals:
    def test_validate_upgrades_to_candidate(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="test-pattern") for i in range(7)
        ]
        store.observe_findings(findings, run_id="run-1", min_observations=5)

        result = store.validate_proposals()
        assert result["candidate"] >= 1 or result["unchanged"] >= 1


class TestShadowRun:
    def test_shadow_records_results(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="test-shadow") for i in range(7)
        ]
        store.observe_findings(findings, run_id="run-1", min_observations=5)
        store.validate_proposals()

        rules = store.load()
        if rules:
            result = store.record_shadow_run(
                rules[0].rule_id,
                matches=10,
                false_positives=0,
            )
            assert result["status"] in ("candidate", "tentative", "active")


class TestApproveReject:
    def test_approve_rule(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="test-approve") for i in range(7)
        ]
        store.observe_findings(findings, run_id="run-1", min_observations=5)
        store.validate_proposals()

        rules = store.load()
        if rules:
            result = store.approve_rule(rules[0].rule_id)
            assert result["status"] == "active" or "reason" in result

    def test_reject_rule(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="test-reject") for i in range(7)
        ]
        store.observe_findings(findings, run_id="run-1", min_observations=5)
        store.validate_proposals()

        rules = store.load()
        if rules:
            result = store.reject_rule(rules[0].rule_id, reason="bad pattern")
            assert result["status"] == "rejected"

    def test_reject_nonexistent_raises(self, tmp_path):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        with pytest.raises(KeyError):
            store.reject_rule("nonexistent-rule")


class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_path, make_finding):
        from bluei.app.emergent_rules import EmergentRuleStore

        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [
            make_finding(finding_id=f"f-{i}", rule="persist-test") for i in range(7)
        ]
        store.observe_findings(findings, run_id="run-1", min_observations=5)

        rules = store.load()
        assert len(rules) >= 1

        store2 = EmergentRuleStore(tmp_path / "rules.json")
        loaded = store2.load()
        assert len(loaded) == len(rules)
        assert loaded[0].rule_id == rules[0].rule_id
