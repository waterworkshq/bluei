import json
from unittest.mock import MagicMock, patch

import pytest

from bluei.app.emergent_rules import (
    DetectionPattern,
    DetectionType,
    EmergentRule,
    EmergentRuleStatus,
    EmergentRuleStore,
    ShadowRuleMatch,
    propose_rules_from_fix_patterns,
    propose_rules_from_findings,
    discover_active_rule_findings,
    scan_shadow_rules,
)
from bluei.engine.models import Finding
from bluei.engine.pattern_store import FixPattern


def _rule(
    rule_id: str, status: EmergentRuleStatus = EmergentRuleStatus.PROPOSED, **kw
) -> EmergentRule:
    defaults = {
        "header": f"Test rule {rule_id}",
        "detection_pattern": DetectionPattern(search_pattern=rule_id),
        "language": "python",
        "category": "lint",
    }
    defaults.update(kw)
    return EmergentRule(rule_id=rule_id, status=status, **defaults)


def _finding(
    finding_id: str,
    rule: str,
    path: str = "src/api/users.py",
    snippet: str = "missing typed dict",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        repo="demo",
        path=path,
        line=10,
        rule=rule,
        snippet=snippet,
        confidence=0.85,
        quick_win=False,
        safe_to_autofix=False,
    )


def test_emergent_rule_store_creates_and_updates_proposals_from_findings(
    tmp_path,
) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")

    result = store.observe_findings(
        [_finding("f-1", "llm-typed-dict"), _finding("f-2", "llm-typed-dict")],
        run_id="run-1",
        min_observations=2,
    )

    assert result["created"] == 1
    assert result["updated"] == 0
    rules = store.load()
    assert len(rules) == 1
    assert rules[0].status == EmergentRuleStatus.PROPOSED
    assert rules[0].observation_count == 2
    assert rules[0].evidence_runs == ["run-1"]
    assert rules[0].source_finding_ids == ["f-1", "f-2"]
    assert rules[0].detection_pattern.search_pattern == "llm-typed-dict"
    assert rules[0].detection_pattern.file_glob == "src/api/**"

    result = store.observe_findings(
        [_finding("f-3", "llm-typed-dict")],
        run_id="run-2",
        min_observations=1,
    )

    assert result["created"] == 0
    assert result["updated"] == 1
    reloaded = store.load()
    assert len(reloaded) == 1
    assert reloaded[0].observation_count == 3
    assert reloaded[0].evidence_runs == ["run-1", "run-2"]
    assert reloaded[0].source_finding_ids == ["f-1", "f-2", "f-3"]


def test_emergent_rule_store_ignores_existing_catalog_rules_and_low_evidence(
    tmp_path,
) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")

    result = store.observe_findings(
        [
            _finding("f-1", "type-explicit-any"),
            _finding("f-2", "llm-one-off"),
        ],
        run_id="run-1",
        existing_rule_ids={"type-explicit-any"},
        min_observations=2,
    )

    assert result == {"created": 0, "updated": 0, "skipped": 2}
    assert store.load() == []


def test_emergent_rules_round_trip_is_backward_compatible(tmp_path) -> None:
    path = tmp_path / "emergent_rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [
                    {
                        "rule_id": "er-abc",
                        "header": "Old rule",
                        "detection_pattern": {
                            "detection_type": "text_pattern",
                            "search_pattern": "old-rule",
                        },
                        "language": "python",
                        "category": "lint",
                        "status": "proposed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = EmergentRuleStore(path).load()

    assert len(rules) == 1
    assert rules[0].rule_id == "er-abc"
    assert rules[0].observation_count == 0
    assert rules[0].detection_pattern.file_glob == "**/*"


def test_propose_rules_from_findings_groups_by_rule_and_directory() -> None:
    proposals = propose_rules_from_findings(
        [
            _finding("f-1", "llm-typed-dict", "src/api/users.py"),
            _finding("f-2", "llm-typed-dict", "src/api/orders.py"),
            _finding("f-3", "llm-typed-dict", "src/ui/app.py"),
        ],
        run_id="run-1",
        min_observations=2,
    )

    assert len(proposals) == 1
    assert proposals[0].detection_pattern.file_glob == "src/api/**"
    assert proposals[0].observation_count == 2


def test_emergent_rule_store_validates_proposals_to_candidate_or_rejected(
    tmp_path,
) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.observe_findings(
        [
            _finding("f-1", "llm-typed-dict"),
            _finding("f-2", "llm-typed-dict"),
            _finding("f-3", "llm-style", "src/ui/app.py"),
            _finding("f-4", "llm-style", "src/ui/nav.py"),
        ],
        run_id="run-1",
        min_observations=2,
    )

    result = store.validate_proposals(existing_rule_ids={"llm-style"})

    assert result == {"candidate": 1, "rejected": 1, "unchanged": 0}
    rules = sorted(store.load(), key=lambda rule: rule.detection_pattern.search_pattern)
    assert rules[0].detection_pattern.search_pattern == "llm-style"
    assert rules[0].status == EmergentRuleStatus.REJECTED
    assert rules[0].rejected_reason == "covered_by_existing_rule"
    assert rules[1].detection_pattern.search_pattern == "llm-typed-dict"
    assert rules[1].status == EmergentRuleStatus.CANDIDATE


def test_emergent_rule_store_records_shadow_runs_and_promotes_safe_rule(
    tmp_path,
) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.observe_findings(
        [_finding("f-1", "llm-typed-dict"), _finding("f-2", "llm-typed-dict")],
        run_id="run-1",
        min_observations=2,
    )
    store.validate_proposals()
    rule = store.load()[0]

    first = store.record_shadow_run(
        rule.rule_id,
        matches=4,
        false_positives=0,
        min_shadow_runs=2,
        max_false_positive_rate=0.2,
    )
    assert first["status"] == "tentative"

    second = store.record_shadow_run(
        rule.rule_id,
        matches=5,
        false_positives=1,
        min_shadow_runs=2,
        max_false_positive_rate=0.2,
    )

    assert second["status"] == "active"
    reloaded = store.load()[0]
    assert reloaded.status == EmergentRuleStatus.ACTIVE
    assert reloaded.shadow_runs == 2
    assert reloaded.shadow_findings == 9
    assert reloaded.false_positive_count == 1
    assert reloaded.false_positive_rate == 1 / 9


def test_emergent_rule_store_rejects_noisy_shadow_rule(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.observe_findings(
        [_finding("f-1", "llm-style"), _finding("f-2", "llm-style")],
        run_id="run-1",
        min_observations=2,
    )
    store.validate_proposals()
    rule = store.load()[0]

    result = store.record_shadow_run(
        rule.rule_id,
        matches=5,
        false_positives=3,
        min_shadow_runs=1,
        max_false_positive_rate=0.2,
    )

    assert result["status"] == "rejected"
    reloaded = store.load()[0]
    assert reloaded.status == EmergentRuleStatus.REJECTED
    assert reloaded.rejected_reason == "shadow_false_positive_rate"


def test_propose_rules_from_fix_patterns_uses_successful_reuse_as_evidence() -> None:
    proposals = propose_rules_from_fix_patterns(
        [
            FixPattern(
                pattern_id="fp-1",
                rule="broad-except",
                language="python",
                file_path="src/api/users.py",
                before_snippet="except:",
                after_snippet="except Exception:",
                diff_patch="-except:\n+except Exception:",
                confidence=0.9,
                success_count=4,
                source_finding_ids=["f-1", "f-2"],
            ),
            FixPattern(
                pattern_id="fp-2",
                rule="broad-except",
                language="python",
                file_path="src/api/orders.py",
                before_snippet="except:",
                after_snippet="except Exception:",
                diff_patch="-except:\n+except Exception:",
                confidence=0.8,
                success_count=3,
                source_finding_ids=["f-3"],
            ),
            FixPattern(
                pattern_id="fp-low",
                rule="rename-var",
                language="python",
                file_path="src/ui/app.py",
                before_snippet="x = 1",
                after_snippet="count = 1",
                diff_patch="-x\n+count",
                confidence=0.9,
                success_count=1,
            ),
        ],
        run_id="patterns-1",
        min_success_count=3,
    )

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.detection_pattern.search_pattern == "broad-except"
    assert proposal.detection_pattern.file_glob == "src/api/**"
    assert proposal.observation_count == 7
    assert proposal.source_finding_ids == ["f-1", "f-2", "f-3"]
    assert "fix patterns" in proposal.notes


def test_emergent_rule_store_observes_fix_patterns(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")

    result = store.observe_fix_patterns(
        [
            FixPattern(
                pattern_id="fp-1",
                rule="broad-except",
                language="python",
                file_path="src/api/users.py",
                before_snippet="except:",
                after_snippet="except Exception:",
                diff_patch="-except:\n+except Exception:",
                confidence=0.9,
                success_count=5,
                source_finding_ids=["f-1"],
            )
        ],
        run_id="patterns-1",
        min_success_count=5,
    )

    assert result == {"created": 1, "updated": 0, "skipped": 0}
    rule = store.load()[0]
    assert rule.detection_pattern.search_pattern == "broad-except"
    assert rule.observation_count == 5


def test_scan_shadow_rules_finds_text_matches_with_relative_paths(tmp_path) -> None:
    worktree = tmp_path / "repo"
    target = worktree / "src" / "api" / "users.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def handle():\n    # llm-typed-dict should be promoted\n", encoding="utf-8"
    )
    (worktree / "src" / "ui").mkdir(parents=True)
    (worktree / "src" / "ui" / "app.py").write_text(
        "# llm-typed-dict outside glob\n", encoding="utf-8"
    )

    matches = scan_shadow_rules(
        [
            EmergentRule(
                rule_id="er-shadow",
                header="Repeated type pattern",
                detection_pattern=DetectionPattern(
                    search_pattern="llm-typed-dict",
                    file_glob="src/api/**",
                ),
                language="python",
                category="type",
                status=EmergentRuleStatus.TENTATIVE,
            )
        ],
        worktree,
    )

    assert len(matches) == 1
    assert matches[0].rule_id == "er-shadow"
    assert matches[0].path == "src/api/users.py"
    assert matches[0].line == 2
    assert matches[0].snippet == "# llm-typed-dict should be promoted"
    assert matches[0].status == EmergentRuleStatus.TENTATIVE


def test_scan_shadow_rules_skips_non_shadowable_rules_and_empty_patterns(
    tmp_path,
) -> None:
    worktree = tmp_path / "repo"
    target = worktree / "src" / "api" / "users.py"
    target.parent.mkdir(parents=True)
    target.write_text("llm-typed-dict\n", encoding="utf-8")

    matches = scan_shadow_rules(
        [
            EmergentRule(
                rule_id="er-proposed",
                header="Proposed",
                detection_pattern=DetectionPattern(search_pattern="llm-typed-dict"),
                language="python",
                category="type",
                status=EmergentRuleStatus.PROPOSED,
            ),
            EmergentRule(
                rule_id="er-rejected",
                header="Rejected",
                detection_pattern=DetectionPattern(search_pattern="llm-typed-dict"),
                language="python",
                category="type",
                status=EmergentRuleStatus.REJECTED,
            ),
            EmergentRule(
                rule_id="er-empty",
                header="Empty",
                detection_pattern=DetectionPattern(search_pattern=""),
                language="python",
                category="type",
                status=EmergentRuleStatus.TENTATIVE,
            ),
        ],
        worktree,
    )

    assert matches == []


def test_emergent_rule_store_rejects_and_retires_rules(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.observe_findings(
        [_finding("f-1", "llm-typed-dict"), _finding("f-2", "llm-typed-dict")],
        run_id="run-1",
        min_observations=2,
    )
    rule_id = store.load()[0].rule_id

    rejected = store.reject_rule(rule_id, reason="too_noisy")

    assert rejected == {"status": "rejected", "updated": True}
    rule = store.load()[0]
    assert rule.status == EmergentRuleStatus.REJECTED
    assert rule.rejected_reason == "too_noisy"

    retired = store.retire_rule(rule_id, reason="superseded")

    assert retired == {"status": "retired", "updated": True}
    reloaded = store.load()[0]
    assert reloaded.status == EmergentRuleStatus.RETIRED
    assert reloaded.rejected_reason == "superseded"
    assert reloaded.retired_at is not None


def test_discover_active_rule_findings_converts_only_active_matches(tmp_path) -> None:
    worktree = tmp_path / "repo"
    target = worktree / "src" / "api" / "users.py"
    target.parent.mkdir(parents=True)
    target.write_text("def handle():\n    llm-typed-dict\n", encoding="utf-8")

    active = EmergentRule(
        rule_id="er-active",
        header="Repeated typed dict",
        detection_pattern=DetectionPattern(
            search_pattern="llm-typed-dict",
            file_glob="src/api/**",
        ),
        language="python",
        category="type",
        status=EmergentRuleStatus.ACTIVE,
        confidence=0.82,
        severity_default="low",
    )
    candidate = EmergentRule(
        rule_id="er-candidate",
        header="Candidate typed dict",
        detection_pattern=DetectionPattern(
            search_pattern="llm-typed-dict",
            file_glob="src/api/**",
        ),
        language="python",
        category="type",
        status=EmergentRuleStatus.CANDIDATE,
    )

    findings = discover_active_rule_findings(
        [active, candidate], worktree, repo_name="demo"
    )

    assert len(findings) == 1
    assert findings[0].finding_id.startswith("erf-")
    assert (
        findings[0].finding_id
        == discover_active_rule_findings(
            [active],
            worktree,
            repo_name="demo",
        )[0].finding_id
    )
    assert findings[0].repo == "demo"
    assert findings[0].path == "src/api/users.py"
    assert findings[0].line == 2
    assert findings[0].rule == "emergent:er-active"
    assert findings[0].snippet == "llm-typed-dict"
    assert findings[0].confidence == 0.82
    assert findings[0].severity == "low"
    assert findings[0].category == "type"
    assert findings[0].quick_win is False
    assert findings[0].safe_to_autofix is False


def test_approve_rule_transitions_to_active(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    for status in (
        EmergentRuleStatus.PROPOSED,
        EmergentRuleStatus.CANDIDATE,
        EmergentRuleStatus.TENTATIVE,
    ):
        store.save([_rule("er-test", status=status)])
        result = store.approve_rule("er-test")
        assert result["updated"] is True
        assert result["status"] == "active"
        rule = store.load()[0]
        assert rule.status == EmergentRuleStatus.ACTIVE
        assert rule.activated_at is not None


def test_approve_rejected_rule_fails(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save([_rule("er-test", status=EmergentRuleStatus.REJECTED)])
    result = store.approve_rule("er-test")
    assert result["updated"] is False


def test_approve_retired_rule_fails(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save([_rule("er-test", status=EmergentRuleStatus.RETIRED)])
    result = store.approve_rule("er-test")
    assert result["updated"] is False


def test_promote_rule_active_to_retired(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save([_rule("er-test", status=EmergentRuleStatus.ACTIVE)])
    result = store.promote_rule("er-test")
    assert result["promoted"] is True
    rule = store.load()[0]
    assert rule.status == EmergentRuleStatus.RETIRED
    assert rule.retired_at is not None


def test_promote_rule_writes_plugin_yaml(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save(
        [_rule("er-test", status=EmergentRuleStatus.ACTIVE, observation_count=10)]
    )
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    result = store.promote_rule("er-test", plugin_dir=plugin_dir)
    assert result["promoted"] is True
    assert result["plugin_written"] is True
    import yaml

    manifest = yaml.safe_load((plugin_dir / "plugin.yaml").read_text())
    rules = manifest["discovery"]["rules"]
    assert len(rules) == 1
    assert rules[0]["id"] == "er-test"
    assert rules[0]["promoted_from"] == "emergent"


def test_promote_rule_from_proposed_fails(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save([_rule("er-test", status=EmergentRuleStatus.PROPOSED)])
    result = store.promote_rule("er-test")
    assert result["promoted"] is False


def test_retire_stale_rules_removes_old_active(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save(
        [
            _rule(
                "er-old",
                status=EmergentRuleStatus.ACTIVE,
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            _rule(
                "er-recent",
                status=EmergentRuleStatus.ACTIVE,
                updated_at="2026-05-19T00:00:00+00:00",
            ),
        ]
    )
    result = store.retire_stale_rules(active_days=60)
    assert result["retired"] >= 1
    rules = store.load()
    old = next(r for r in rules if r.rule_id == "er-old")
    recent = next(r for r in rules if r.rule_id == "er-recent")
    assert old.status == EmergentRuleStatus.RETIRED
    assert recent.status == EmergentRuleStatus.ACTIVE


def test_retire_stale_rules_caps_at_max_active(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    rules = [
        _rule(
            f"er-{i}",
            status=EmergentRuleStatus.ACTIVE,
            updated_at="2026-05-19T00:00:00+00:00",
        )
        for i in range(5)
    ]
    store.save(rules)
    result = store.retire_stale_rules(max_active=2)
    assert result["retired"] == 3
    active = [r for r in store.load() if r.status == EmergentRuleStatus.ACTIVE]
    assert len(active) == 2


def test_retire_stale_proposed_rules(tmp_path) -> None:
    store = EmergentRuleStore(tmp_path / "emergent_rules.json")
    store.save(
        [
            _rule(
                "er-stale-proposed",
                status=EmergentRuleStatus.PROPOSED,
                updated_at="2026-01-01T00:00:00+00:00",
            ),
            _rule(
                "er-fresh-proposed",
                status=EmergentRuleStatus.PROPOSED,
                updated_at="2026-05-19T00:00:00+00:00",
            ),
        ]
    )
    result = store.retire_stale_rules(proposed_days=30)
    assert result["retired"] >= 1
    rules = store.load()
    stale = next(r for r in rules if r.rule_id == "er-stale-proposed")
    fresh = next(r for r in rules if r.rule_id == "er-fresh-proposed")
    assert stale.status == EmergentRuleStatus.RETIRED
    assert fresh.status == EmergentRuleStatus.PROPOSED


# ── Tests extracted from test_phase5_modules_remaining.py ──


class TestEmergentRulesObserveFixPatternsUpdate:
    """observe_fix_patterns updates an existing rule when pattern matches."""

    def test_observe_fix_patterns_updates_existing_rule(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        pattern1 = MagicMock()
        pattern1.pattern_id = "fp-1"
        pattern1.rule = "broad-except"
        pattern1.language = "python"
        pattern1.file_path = "src/api/users.py"
        pattern1.confidence = 0.9
        pattern1.success_count = 5
        pattern1.source_finding_ids = ["f-1"]
        store.observe_fix_patterns([pattern1], run_id="run-1", min_success_count=5)
        pattern2 = MagicMock()
        pattern2.pattern_id = "fp-2"
        pattern2.rule = "broad-except"
        pattern2.language = "python"
        pattern2.file_path = "src/api/orders.py"
        pattern2.confidence = 0.85
        pattern2.success_count = 6
        pattern2.source_finding_ids = ["f-2"]
        result = store.observe_fix_patterns(
            [pattern2], run_id="run-2", min_success_count=5
        )
        assert result["created"] == 0
        assert result["updated"] == 1


class TestEmergentRulesValidateUnchanged:
    """validate_proposals counts non-proposed rules as unchanged."""

    def test_validate_proposals_counts_non_proposed_as_unchanged(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save(
            [
                _rule("er-candidate", status=EmergentRuleStatus.CANDIDATE),
                _rule("er-active", status=EmergentRuleStatus.ACTIVE),
            ]
        )
        result = store.validate_proposals()
        assert result["unchanged"] == 2
        assert result["candidate"] == 0
        assert result["rejected"] == 0


class TestEmergentRulesRecordShadowRun:
    """record_shadow_run transitions and edge cases."""

    def test_record_shadow_run_candidate_to_tentative(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save([_rule("er-test", status=EmergentRuleStatus.CANDIDATE)])
        result = store.record_shadow_run(
            "er-test", matches=3, false_positives=0, min_shadow_runs=5
        )
        assert result["status"] == "tentative"
        assert result["updated"] is True
        assert result["shadow_runs"] == 1

    def test_record_shadow_run_non_tentative_returns_not_updated(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save([_rule("er-test", status=EmergentRuleStatus.ACTIVE)])
        result = store.record_shadow_run("er-test", matches=3, false_positives=0)
        assert result["updated"] is False
        assert result["status"] == "active"

    def test_record_shadow_run_raises_for_missing_rule(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        with pytest.raises(KeyError):
            store.record_shadow_run("er-nonexistent", matches=1, false_positives=0)


class TestEmergentRulesRejectRaises:
    """reject_rule raises for missing rule."""

    def test_reject_rule_raises_for_missing(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")

        with pytest.raises(KeyError):
            store.reject_rule("er-nonexistent")


class TestEmergentRulesApproveEdgeCases:
    """approve_rule edge cases."""

    def test_approve_rule_raises_for_missing(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")

        with pytest.raises(KeyError):
            store.approve_rule("er-nonexistent")

    def test_approve_active_rule_returns_not_updated(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save([_rule("er-test", status=EmergentRuleStatus.ACTIVE)])
        result = store.approve_rule("er-test")
        assert result["updated"] is False
        assert "cannot approve" in result["reason"]


class TestEmergentRulesRetireRaises:
    """retire_rule raises for missing rule."""

    def test_retire_rule_raises_for_missing(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")

        with pytest.raises(KeyError):
            store.retire_rule("er-nonexistent")


class TestEmergentRulesPromoteEdgeCases:
    """promote_rule edge cases."""

    def test_promote_rule_raises_for_missing(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")

        with pytest.raises(KeyError):
            store.promote_rule("er-nonexistent")

    def test_promote_rule_handles_corrupt_plugin_yaml(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save([_rule("er-test", status=EmergentRuleStatus.ACTIVE)])
        plugin_dir = tmp_path / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("key: value\n", encoding="utf-8")
        with patch("yaml.safe_load", side_effect=Exception("yaml broke")):
            result = store.promote_rule("er-test", plugin_dir=plugin_dir)
        assert result["promoted"] is True
        assert result["plugin_written"] is True


class TestEmergentRulesRetireStaleBadDate:
    """retire_stale_rules handles unparseable date strings."""

    def test_retire_stale_rules_handles_bad_date(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save(
            [
                _rule(
                    "er-bad-date",
                    status=EmergentRuleStatus.ACTIVE,
                    updated_at="not-a-date",
                ),
            ]
        )
        result = store.retire_stale_rules(active_days=60)
        assert result["retired"] == 0


class TestScanShadowRulesEdgeCases:
    """scan_shadow_rules edge cases."""

    def test_scan_shadow_skips_structural_detection_type(self, tmp_path):
        worktree = tmp_path / "repo"
        (worktree / "src").mkdir(parents=True)
        (worktree / "src" / "a.py").write_text("TODO\n", encoding="utf-8")
        rule = EmergentRule(
            rule_id="er-struct",
            header="Structural",
            detection_pattern=DetectionPattern(
                detection_type=DetectionType.STRUCTURAL,
                search_pattern="TODO",
            ),
            language="python",
            category="lint",
            status=EmergentRuleStatus.TENTATIVE,
        )
        assert scan_shadow_rules([rule], worktree) == []

    def test_scan_shadow_skips_non_file_paths(self, tmp_path):
        worktree = tmp_path / "repo"
        (worktree / "src").mkdir(parents=True)
        rule = EmergentRule(
            rule_id="er-dir",
            header="Dir match",
            detection_pattern=DetectionPattern(
                search_pattern="anything",
                file_glob="**/*",
            ),
            language="python",
            category="lint",
            status=EmergentRuleStatus.ACTIVE,
        )
        with patch.object(__import__("pathlib").Path, "is_file", return_value=False):
            matches = scan_shadow_rules([rule], worktree)
        assert matches == []

    def test_scan_shadow_handles_oserror(self, tmp_path):
        worktree = tmp_path / "repo"
        (worktree / "src").mkdir(parents=True)
        target = worktree / "src" / "a.py"
        target.write_text("needle_here\n", encoding="utf-8")
        rule = EmergentRule(
            rule_id="er-ose",
            header="OSError",
            detection_pattern=DetectionPattern(search_pattern="needle_here"),
            language="python",
            category="lint",
            status=EmergentRuleStatus.ACTIVE,
        )
        with patch.object(
            __import__("pathlib").Path, "read_text", side_effect=OSError("nope")
        ):
            matches = scan_shadow_rules([rule], worktree)
        assert matches == []


class TestDiscoverActiveRuleFindingsNoRule:
    """discover_active_rule_findings skips matches whose rule_id is not in the active list."""

    def test_discover_skips_when_rule_not_found(self, tmp_path):
        worktree = tmp_path / "repo"
        (worktree / "src").mkdir(parents=True)
        (worktree / "src" / "a.py").write_text("findme\n", encoding="utf-8")
        other_rule = EmergentRule(
            rule_id="er-other",
            header="Other",
            detection_pattern=DetectionPattern(search_pattern="findme"),
            language="python",
            category="lint",
            status=EmergentRuleStatus.ACTIVE,
        )
        with patch(
            "bluei.app.emergent_rules.scan_shadow_rules",
            return_value=[
                ShadowRuleMatch(
                    rule_id="er-missing",
                    path="src/a.py",
                    line=1,
                    snippet="findme",
                    status=EmergentRuleStatus.ACTIVE,
                )
            ],
        ):
            findings = discover_active_rule_findings(
                [other_rule], worktree, repo_name="demo"
            )
        assert findings == []


class TestProposeFixPatternsLowConfidence:
    """propose_rules_from_fix_patterns skips low-confidence patterns."""

    def test_propose_fix_patterns_skips_low_confidence(self):
        pattern = MagicMock()
        pattern.success_count = 10
        pattern.confidence = 0.3
        pattern.rule = "test-rule"
        pattern.file_path = "src/a.py"
        pattern.source_finding_ids = []
        result = propose_rules_from_fix_patterns(
            [pattern], run_id="r1", min_success_count=5
        )
        assert result == []


class TestShadowRuleMatchToDict:
    """ShadowRuleMatch.to_dict() produces the expected shape."""

    def test_shadow_rule_match_to_dict(self):
        match = ShadowRuleMatch(
            rule_id="er-abc",
            path="src/a.py",
            line=5,
            snippet="code here",
            status=EmergentRuleStatus.TENTATIVE,
        )
        d = match.to_dict()
        assert d["rule_id"] == "er-abc"
        assert d["status"] == "tentative"
        assert d["source"] == "emergent-shadow"
        assert d["line"] == 5
        assert d["snippet"] == "code here"


class TestEmergentRulesLoopContinues:
    """Multi-rule operations find the correct target after skipping others."""

    def test_record_shadow_run_finds_rule_after_skip(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save(
            [
                _rule("er-first", status=EmergentRuleStatus.PROPOSED),
                _rule("er-second", status=EmergentRuleStatus.CANDIDATE),
            ]
        )
        result = store.record_shadow_run(
            "er-second", matches=1, false_positives=0, min_shadow_runs=5
        )
        assert result["status"] == "tentative"
        assert result["updated"] is True

    def test_reject_rule_finds_after_skip(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save(
            [
                _rule("er-first", status=EmergentRuleStatus.PROPOSED),
                _rule("er-second", status=EmergentRuleStatus.PROPOSED),
            ]
        )
        result = store.reject_rule("er-second")
        assert result["updated"] is True
        rules = store.load()
        assert rules[1].status == EmergentRuleStatus.REJECTED

    def test_approve_rule_finds_after_skip(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save(
            [
                _rule("er-first", status=EmergentRuleStatus.PROPOSED),
                _rule("er-second", status=EmergentRuleStatus.CANDIDATE),
            ]
        )
        result = store.approve_rule("er-second")
        assert result["updated"] is True
        assert result["status"] == "active"

    def test_retire_rule_finds_after_skip(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        store.save(
            [
                _rule("er-first", status=EmergentRuleStatus.ACTIVE),
                _rule("er-second", status=EmergentRuleStatus.ACTIVE),
            ]
        )
        result = store.retire_rule("er-second")
        assert result["updated"] is True
        rules = store.load()
        assert rules[1].status == EmergentRuleStatus.RETIRED

    def test_observe_findings_with_skipped_entries(self, tmp_path):
        store = EmergentRuleStore(tmp_path / "rules.json")
        findings = [_finding(f"f-{i}", "some-rule") for i in range(6)]
        result = store.observe_findings(findings, run_id="run-1", min_observations=5)
        assert result["created"] == 1
        assert result["skipped"] == 0
