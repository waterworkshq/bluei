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


class TestIntegratedLifecycle:
    """Full lifecycle chains that cross multiple transitions in one test.

    These complement the single-transition tests above by exercising the
    observe -> validate -> shadow -> approve -> scan path end-to-end against
    a real ``EmergentRuleStore`` backed by a JSON file on ``tmp_path`` and a
    real on-disk repository tree.
    """

    def test_full_emergent_lifecycle(self, tmp_path, make_finding):
        """observe -> validate -> shadow (mixed TP/FP) -> approve -> scan.

        Verifies that an ACTIVE rule produces real ``Finding`` objects whose
        ``rule`` field carries the ``emergent:`` prefix when scanned against a
        repository that contains the rule's search pattern.

        Note: ``propose_rules_from_findings`` derives a regex detection
        pattern from the finding snippet (ADR-0021). The on-disk repo content
        must satisfy that regex for ``scan_shadow_rules`` to produce matches.
        """
        import textwrap

        from bluei.app.emergent_rules import (
            EmergentRuleStore,
            EmergentRuleStatus,
            discover_active_rule_findings,
            scan_shadow_rules,
        )

        # Arrange: a real repo whose files contain the rule's search pattern.
        # ``rule="shell=True"`` is chosen so the pattern literally appears in
        # the source files, letting the shadow scan find genuine matches.
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "helpers.py").write_text(
            textwrap.dedent(
                """\
                import subprocess


                def run(cmd):
                    subprocess.run(cmd, shell=True)


                def run_safely(cmd):
                    subprocess.run(cmd, shell=True)
                """
            )
        )

        store = EmergentRuleStore(tmp_path / "rules.json")

        # 1) Observe: 6 findings with the same rule in the same dir glob.
        # ``propose_rules_from_findings`` extracts a regex detection pattern
        # from the finding snippet (ADR-0021); the resulting regex still
        # matches the literal ``shell=True`` lines written below.
        findings = [
            make_finding(
                finding_id=f"f-life-{i}",
                rule="shell=True",
                path=f"src/mod_{i}.py",
                snippet="subprocess.run(cmd, shell=True)",
            )
            for i in range(6)
        ]
        observed = store.observe_findings(
            findings, run_id="run-life-001", min_observations=5
        )
        assert observed["created"] == 1

        rules = store.load()
        proposed = [r for r in rules if r.status == EmergentRuleStatus.PROPOSED]
        assert len(proposed) == 1
        rule_id = proposed[0].rule_id
        # Detection pattern is a regex that generalizes the snippet's
        # identifiers while preserving the ``shell=True`` literal.
        from bluei.app.emergent_rules import DetectionType

        assert (
            proposed[0].detection_pattern.detection_type == DetectionType.REGEX_PATTERN
        )
        assert r"\w+" in proposed[0].detection_pattern.search_pattern
        assert "shell=True" in proposed[0].detection_pattern.search_pattern

        # 2) Validate: PROPOSED -> CANDIDATE.
        validated = store.validate_proposals()
        assert validated["candidate"] == 1
        candidate = next(r for r in store.load() if r.rule_id == rule_id)
        assert candidate.status == EmergentRuleStatus.CANDIDATE

        # 3) Shadow runs: mix of true positives and false positives. Two runs
        #    accumulate evidence but stay TENTATIVE (fewer than min_shadow_runs).
        run1 = store.record_shadow_run(
            rule_id,
            matches=8,
            false_positives=1,
            min_shadow_runs=3,
            max_false_positive_rate=0.2,
        )
        assert run1["status"] == "tentative"
        assert run1["shadow_runs"] == 1

        run2 = store.record_shadow_run(
            rule_id,
            matches=6,
            false_positives=0,
            min_shadow_runs=3,
            max_false_positive_rate=0.2,
        )
        assert run2["status"] == "tentative"
        assert run2["shadow_runs"] == 2

        target = next(r for r in store.load() if r.rule_id == rule_id)
        assert target.shadow_findings == 14  # 8 + 6
        assert target.false_positive_count == 1  # cumulative
        assert target.false_positive_rate == 1 / 14

        # 4) Approve: evidence is strong (low cumulative FP rate), so a human
        #    promotes TENTATIVE -> ACTIVE.
        approval = store.approve_rule(rule_id)
        assert approval == {"status": "active", "updated": True}

        active_rules = [
            r for r in store.load() if r.status == EmergentRuleStatus.ACTIVE
        ]
        assert len(active_rules) == 1
        assert active_rules[0].activated_at is not None

        # 5) Scan the repo with the ACTIVE rule.
        matches = scan_shadow_rules(active_rules, repo)
        assert len(matches) == 2  # two shell=True lines in helpers.py
        assert {m.path for m in matches} == {"src/helpers.py"}
        assert all(m.rule_id == rule_id for m in matches)

        # 6) Discover findings: each match becomes a Finding with emergent: prefix.
        discovered = discover_active_rule_findings(
            active_rules, repo, repo_name="test-repo"
        )
        assert len(discovered) == 2
        for finding in discovered:
            assert finding.rule.startswith("emergent:")
            assert finding.rule == f"emergent:{rule_id}"
            assert finding.repo == "test-repo"
            assert finding.path == "src/helpers.py"
            assert finding.finding_id.startswith("erf-")

    def test_cumulative_false_positives_reject_rule(self, tmp_path, make_finding):
        """Three shadow runs accumulating 5 FP / 18 matches (27.8%) -> REJECTED.

        The cumulative false-positive rate exceeds ``max_false_positive_rate``
        only after the third run completes (5/18 = 0.278 > 0.2), so the rule
        must remain TENTATIVE through runs 1 and 2 and flip to REJECTED on run
        3 with the canonical ``shadow_false_positive_rate`` reason.
        """
        from bluei.app.emergent_rules import (
            EmergentRuleStore,
            EmergentRuleStatus,
        )

        store = EmergentRuleStore(tmp_path / "rules.json")

        # Arrange + observe: a candidate rule on a "clean" codebase (findings
        # exist, but real matches will all turn out to be false positives).
        findings = [
            make_finding(
                finding_id=f"f-fp-{i}",
                rule="dbg-print",
                path=f"src/mod_{i}.py",
                snippet="print('debug')",
            )
            for i in range(6)
        ]
        observed = store.observe_findings(
            findings, run_id="run-fp-001", min_observations=5
        )
        assert observed["created"] == 1

        result = store.validate_proposals()
        assert result["candidate"] == 1
        rules = store.load()
        rule_id = rules[0].rule_id
        assert rules[0].status == EmergentRuleStatus.CANDIDATE

        # Act: three shadow runs. (8,3) + (5,2) + (5,0) = 18 matches, 5 FP.
        run1 = store.record_shadow_run(
            rule_id,
            matches=8,
            false_positives=3,
            min_shadow_runs=3,
            max_false_positive_rate=0.2,
        )
        run2 = store.record_shadow_run(
            rule_id,
            matches=5,
            false_positives=2,
            min_shadow_runs=3,
            max_false_positive_rate=0.2,
        )
        run3 = store.record_shadow_run(
            rule_id,
            matches=5,
            false_positives=0,
            min_shadow_runs=3,
            max_false_positive_rate=0.2,
        )

        # Assert intermediate states held at TENTATIVE.
        assert run1["status"] == "tentative"
        assert run1["shadow_runs"] == 1
        assert run2["status"] == "tentative"
        assert run2["shadow_runs"] == 2

        # Final state: cumulative FP rate 5/18 exceeds threshold -> REJECTED.
        target = next(r for r in store.load() if r.rule_id == rule_id)
        assert target.status == EmergentRuleStatus.REJECTED
        assert target.rejected_reason == "shadow_false_positive_rate"
        assert target.shadow_runs == 3
        assert target.shadow_findings == 18
        assert target.false_positive_count == 5
        assert abs(target.false_positive_rate - (5 / 18)) < 1e-9

    def test_rule_promotion_writes_audit_record(self, tmp_path, make_finding):
        """Approve -> promote_rule writes a plugin.yaml audit entry and retires the rule.

        After promotion the rule must be persisted as RETIRED (re-loadable from
        disk) and the plugin manifest must contain a new discovery entry with
        the canonical promoted-from-emergent fields and an evidence_count that
        mirrors the rule's observation_count.
        """
        import yaml

        from bluei.app.emergent_rules import (
            EmergentRuleStore,
            EmergentRuleStatus,
        )

        store = EmergentRuleStore(tmp_path / "rules.json")

        # Arrange: drive a real rule through observe -> validate -> approve.
        findings = [
            make_finding(
                finding_id=f"f-promo-{i}",
                rule="todo-XXX",
                path=f"src/mod_{i}.py",
                snippet="# TODO: fix",
            )
            for i in range(6)
        ]
        store.observe_findings(findings, run_id="run-promo-001", min_observations=5)
        store.validate_proposals()

        rules = store.load()
        rule_id = rules[0].rule_id
        observation_count = rules[0].observation_count
        assert observation_count == 6

        approval = store.approve_rule(rule_id)
        assert approval == {"status": "active", "updated": True}

        # Pre-existing plugin manifest so we can verify append (not overwrite).
        plugin_dir = tmp_path / "plugins" / "python"
        plugin_dir.mkdir(parents=True)
        existing_manifest = {
            "name": "python-plugin",
            "version": "1.0",
            "discovery": {
                "rules": [
                    {"id": "existing-rule", "category": "lint", "confidence": 0.9},
                ]
            },
        }
        (plugin_dir / "plugin.yaml").write_text(yaml.dump(existing_manifest))

        # Act: promote the ACTIVE rule into the plugin manifest.
        result = store.promote_rule(rule_id, plugin_dir=plugin_dir)
        assert result["promoted"] is True
        assert result["plugin_written"] is True
        assert result["rule_id"] == rule_id

        # Assert: rule is RETIRED and the audit transition is persisted to disk.
        promoted_rule = next(r for r in store.load() if r.rule_id == rule_id)
        assert promoted_rule.status == EmergentRuleStatus.RETIRED
        assert promoted_rule.retired_at is not None

        # Reload from disk to confirm the audit record actually survived a save.
        reloaded_store = EmergentRuleStore(tmp_path / "rules.json")
        reloaded_rule = next(r for r in reloaded_store.load() if r.rule_id == rule_id)
        assert reloaded_rule.status == EmergentRuleStatus.RETIRED
        assert reloaded_rule.retired_at == promoted_rule.retired_at

        # Assert: plugin.yaml contains both the existing and the new audit entry.
        with (plugin_dir / "plugin.yaml").open() as handle:
            manifest = yaml.safe_load(handle)
        discovery_rules = manifest["discovery"]["rules"]
        assert len(discovery_rules) == 2

        # Existing entry untouched.
        assert discovery_rules[0]["id"] == "existing-rule"

        # New promoted entry carries the canonical audit fields.
        audit_entry = discovery_rules[-1]
        assert audit_entry["id"] == rule_id
        assert audit_entry["category"] == "lint"
        assert audit_entry["confidence"] == 0.85
        assert audit_entry["autofix"] is False
        assert audit_entry["promoted_from"] == "emergent"
        assert audit_entry["evidence_count"] == observation_count
