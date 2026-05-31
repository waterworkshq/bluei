#!/usr/bin/env python3
"""Tests for bluei/app/models.py standalone functions and model round-trips."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bluei.engine.models import Finding
from bluei.app.models import (
    generate_id,
    now_iso,
    HealthScore,
    Baseline,
    Repo,
    RepoConfig,
    RepoStatus,
    SafetyMode,
    SafetyProfile,
    ReviewMode,
)


class TestGenerateId:
    def test_generate_id_no_prefix(self):
        id1 = generate_id()
        id2 = generate_id()
        assert id1 and id2
        assert id1 != id2  # unique

    def test_generate_id_with_prefix(self):
        id_val = generate_id("qa")
        assert id_val.startswith("qa-")
        assert len(id_val.split("-")) >= 3  # prefix, ts, unique

    def test_generate_id_prefix_only(self):
        id_val = generate_id("test")
        parts = id_val.split("-")
        assert parts[0] == "test"


class TestNowIso:
    def test_now_iso_returns_string(self):
        result = now_iso()
        assert isinstance(result, str)
        assert result  # not empty

    def test_now_iso_contains_z(self):
        result = now_iso()
        assert "Z" in result or "+" in result  # ISO format with timezone

    def test_now_iso_reasonable_length(self):
        result = now_iso()
        assert 20 <= len(result) <= 35  # ISO with microseconds can be 32+


class TestHealthScoreBand:
    def test_band_excellent(self):
        hs = HealthScore(
            score=95.0, components={}, calculated_at="2026-03-22T00:00:00Z"
        )
        assert hs.band == "excellent"
        assert hs.color == "green"

    def test_band_good(self):
        hs = HealthScore(
            score=80.0, components={}, calculated_at="2026-03-22T00:00:00Z"
        )
        assert hs.band == "good"
        assert hs.color == "blue"

    def test_band_needs_work(self):
        hs = HealthScore(
            score=60.0, components={}, calculated_at="2026-03-22T00:00:00Z"
        )
        assert hs.band == "needs_work"
        assert hs.color == "yellow"

    def test_band_poor(self):
        hs = HealthScore(
            score=40.0, components={}, calculated_at="2026-03-22T00:00:00Z"
        )
        assert hs.band == "poor"
        assert hs.color == "orange"

    def test_band_critical(self):
        hs = HealthScore(
            score=15.0, components={}, calculated_at="2026-03-22T00:00:00Z"
        )
        assert hs.band == "critical"
        assert hs.color == "red"

    def test_band_at_thresholds(self):
        for score, expected_band in [
            (90, "excellent"),
            (70, "good"),
            (50, "needs_work"),
            (30, "poor"),
            (0, "critical"),
        ]:
            hs = HealthScore(
                score=score, components={}, calculated_at="2026-03-22T00:00:00Z"
            )
            assert hs.band == expected_band, f"score={score}"


class TestFindingRoundTrip:
    def test_finding_to_dict(self):
        f = Finding(
            finding_id="fid-123",
            repo="ky",
            path="src/main.ts",
            line=42,
            rule="complexity",
            snippet="Method too complex",
            confidence=0.8,
            quick_win=False,
            safe_to_autofix=True,
        )
        d = f.to_dict()
        assert d["finding_id"] == "fid-123"
        assert d["rule"] == "complexity"
        assert d["confidence"] == 0.8

    def test_finding_from_dict(self):
        d = {
            "finding_id": "fid-456",
            "repo": "ky",
            "path": "src/main.ts",
            "line": 10,
            "rule": "max-lines",
            "snippet": "Too many lines",
            "confidence": 0.75,
            "quick_win": True,
            "safe_to_autofix": True,
        }
        f = Finding.from_dict(d)
        assert f.finding_id == "fid-456"
        assert f.rule == "max-lines"

    def test_finding_from_dict_ignores_unknown_fields(self):
        d = {
            "finding_id": "fid-789",
            "repo": "ky",
            "path": "src/main.ts",
            "line": 11,
            "rule": "xo-no-warning-comments",
            "snippet": "TODO",
            "confidence": 0.8,
            "quick_win": True,
            "safe_to_autofix": True,
            "fix_attempts": 2,
            "last_fix_error": "boom",
            "unexpected_future_field": "ignore-me",
        }
        f = Finding.from_dict(d)
        assert f.finding_id == "fid-789"
        assert f.fix_attempts == 2
        assert f.last_fix_error == "boom"


class TestHealthScoreRoundTrip:
    def test_health_score_to_dict(self):
        hs = HealthScore(
            score=85.0,
            components={"lint": 90.0, "complexity": 80.0},
            calculated_at="2026-03-22T00:00:00Z",
        )
        d = hs.to_dict()
        assert d["score"] == 85.0
        assert d["band"] == "good"
        assert d["color"] == "blue"
        assert d["components"]["lint"] == 90.0


class TestBaselineRoundTrip:
    def test_baseline_to_dict(self):
        b = Baseline(
            id="baseline-1",
            repo_id="repo-ky",
            captured_at="2026-03-22T00:00:00Z",
            findings_total=10,
            findings_by_category={"lint": 5, "complexity": 5},
            findings_by_severity={"medium": 8, "low": 2},
            health_score=85.0,
            health_components={"lint": 90.0},
            findings_file="/tmp/findings.jsonl",
        )
        d = b.to_dict()
        assert d["findings_total"] == 10
        assert d["health_score"] == 85.0


class TestRepoStatusEnum:
    def test_status_values(self):
        assert RepoStatus.IDLE.value == "idle"
        assert RepoStatus.RUNNING.value == "running"
        assert RepoStatus.READY.value == "ready"
        assert RepoStatus.PAUSED.value == "paused"
        assert RepoStatus.ERROR.value == "error"


class TestSafetyModeEnum:
    def test_safety_mode_values(self):
        assert SafetyMode.OBSERVE.value == "observe"
        assert SafetyMode.ISSUE_ONLY.value == "issue-only"
        assert SafetyMode.PR.value == "pr"
        assert SafetyMode.MERGE.value == "merge"


class TestSafetyProfileEnum:
    def test_safety_profile_values(self):
        assert SafetyProfile.CONSERVATIVE.value == "conservative"
        assert SafetyProfile.BALANCED.value == "balanced"
        assert SafetyProfile.AGGRESSIVE.value == "aggressive"


class TestReviewModeEnum:
    def test_review_mode_values(self):
        assert ReviewMode.OBSERVATION.value == "observation"
        assert ReviewMode.AUTONOMOUS_REVIEW.value == "autonomous-review"
        assert ReviewMode.REMEDIATION.value == "remediation"

    def test_repo_config_default_review_mode(self):
        cfg = RepoConfig(
            id="repo-test",
            name="test",
            path="/tmp/test",
            language="python",
        )
        assert cfg.review_care.get("mode") == "observation"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])


# ── Merged from test_engine_models_remaining.py ──

from bluei.engine.models import (
    Finding as EngineFinding,
    BatchStatus,
    FixResult,
    BatchRule,
    BatchGroup,
    now_iso as engine_now_iso,
    parse_iso,
    age_seconds,
    stable_finding_id,
)


def _engine_finding(**overrides):
    defaults = {
        "finding_id": "fid-1",
        "repo": "test-repo",
        "path": "src/main.py",
        "line": 42,
        "rule": "ruff-c408",
        "snippet": "x = tuple(1, 2)",
        "confidence": 0.9,
        "quick_win": True,
        "safe_to_autofix": True,
    }
    defaults.update(overrides)
    return EngineFinding(**defaults)


class TestEngineFindingAsDictNewFields:
    def test_fix_attempts_serialized_when_nonzero(self):
        f = _engine_finding(fix_attempts=3)
        d = f.as_dict()
        assert d["fix_attempts"] == 3

    def test_fix_attempts_serialized_when_zero(self):
        f = _engine_finding(fix_attempts=0)
        d = f.as_dict()
        assert d["fix_attempts"] == 0

    def test_last_fix_error_serialized(self):
        f = _engine_finding(last_fix_error="timeout")
        d = f.as_dict()
        assert d["last_fix_error"] == "timeout"

    def test_last_fix_at_serialized(self):
        f = _engine_finding(last_fix_at="2026-01-01T00:00:00Z")
        d = f.as_dict()
        assert d["last_fix_at"] == "2026-01-01T00:00:00Z"

    def test_fix_success_serialized_when_true(self):
        f = _engine_finding(fix_success=True)
        d = f.as_dict()
        assert d["fix_success"] is True

    def test_fix_success_serialized_when_false(self):
        f = _engine_finding(fix_success=False)
        d = f.as_dict()
        assert d["fix_success"] is False

    def test_refactor_class_serialized(self):
        f = _engine_finding(refactor_class="simple_fix")
        d = f.as_dict()
        assert d["refactor_class"] == "simple_fix"

    def test_refactor_phase_serialized(self):
        f = _engine_finding(refactor_phase="planning")
        d = f.as_dict()
        assert d["refactor_phase"] == "planning"

    def test_discovered_at_serialized(self):
        f = _engine_finding(discovered_at="2026-01-01T00:00:00Z")
        d = f.as_dict()
        assert d["discovered_at"] == "2026-01-01T00:00:00Z"

    def test_severity_serialized(self):
        f = _engine_finding(severity="high")
        d = f.as_dict()
        assert d["severity"] == "high"

    def test_category_serialized(self):
        f = _engine_finding(category="lint")
        d = f.as_dict()
        assert d["category"] == "lint"

    def test_as_dict_equals_to_dict(self):
        f = _engine_finding(severity="high", category="lint")
        assert f.as_dict() == f.to_dict()


class TestEngineFindingFromDictRoundTrip:
    def test_round_trip_preserves_new_fields(self):
        f = _engine_finding(
            fix_attempts=2,
            last_fix_error="err",
            fix_success=True,
            refactor_class="claude_fix",
            refactor_phase="done",
        )
        d = f.as_dict()
        f2 = EngineFinding.from_dict(d)
        assert f2.fix_attempts == 2
        assert f2.last_fix_error == "err"
        assert f2.fix_success is True
        assert f2.refactor_class == "claude_fix"
        assert f2.refactor_phase == "done"

    def test_from_dict_with_missing_new_fields(self):
        d = {
            "finding_id": "fid-x",
            "repo": "r",
            "path": "p",
            "line": 1,
            "rule": "rule",
            "snippet": "snip",
            "confidence": 0.5,
            "quick_win": False,
            "safe_to_autofix": False,
        }
        f = EngineFinding.from_dict(d)
        assert f.fix_attempts == 0
        assert f.last_fix_error is None
        assert f.last_fix_at is None
        assert f.fix_success is False
        assert f.refactor_class is None
        assert f.refactor_phase is None
        assert f.discovered_at is None
        assert f.severity == "medium"
        assert f.category == ""

    def test_from_dict_ignores_unknown_keys(self):
        d = {
            "finding_id": "fid-x",
            "repo": "r",
            "path": "p",
            "line": 1,
            "rule": "rule",
            "snippet": "snip",
            "confidence": 0.5,
            "quick_win": False,
            "safe_to_autofix": False,
            "unknown_field": "ignored",
        }
        f = EngineFinding.from_dict(d)
        assert f.finding_id == "fid-x"


class TestBatchStatus:
    def test_all_values(self):
        expected = {
            "OPEN",
            "FIXING",
            "FIXING_PARTIAL",
            "PR_CREATED",
            "PR_MERGED",
            "FAILED",
            "SPLIT",
            "ABORTED",
            "DRY_RUN",
            "SKIPPED",
        }
        actual = {s.name for s in BatchStatus}
        assert actual == expected


class TestFixResult:
    def test_defaults(self):
        r = FixResult(finding_id="fid-1", status="success")
        assert r.diff_lines == 0
        assert r.error is None
        assert r.fix_method == "autofix"


class TestBatchRule:
    def test_defaults(self):
        r = BatchRule(rule_pattern="ruff-*")
        assert r.enabled is True
        assert r.group_by == "rule"
        assert r.max_batch_size == 20
        assert r.severity == "normal"


class TestBatchGroup:
    def test_is_solo_true(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="solo",
            findings=[f],
            issues=[{"finding_id": "fid-1"}],
        )
        assert bg.is_solo

    def test_is_solo_false(self):
        f1 = _engine_finding()
        f2 = _engine_finding(finding_id="fid-2", line=50)
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="rule",
            findings=[f1, f2],
            issues=[{}, {}],
        )
        assert not bg.is_solo

    def test_file_count(self):
        f1 = _engine_finding(path="a.py")
        f2 = _engine_finding(path="b.py", finding_id="fid-2")
        f3 = _engine_finding(path="a.py", finding_id="fid-3", line=99)
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="rule",
            findings=[f1, f2, f3],
            issues=[{}, {}, {}],
        )
        assert bg.file_count == 2

    def test_pr_title_solo(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="ruff-c408",
            group_by="solo",
            findings=[f],
            issues=[{}],
        )
        assert "fix: resolve ruff-c408" in bg.pr_title()

    def test_pr_title_same_rule_batch(self):
        f1 = _engine_finding()
        f2 = _engine_finding(finding_id="fid-2", line=50)
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="ruff-c408",
            group_by="rule",
            findings=[f1, f2],
            issues=[{}, {}],
        )
        assert "2 ruff-c408 specks" in bg.pr_title()

    def test_pr_title_cross_rule(self):
        f1 = _engine_finding(rule="ruff-c408")
        f2 = _engine_finding(finding_id="fid-2", rule="xo-no-any", line=50)
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="mixed",
            group_by="cross-rule",
            findings=[f1, f2],
            issues=[{}, {}],
        )
        assert "2 linter specks" in bg.pr_title()
        assert "2 rules" in bg.pr_title()

    def test_pr_body_solo(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="ruff-c408",
            group_by="solo",
            findings=[f],
            issues=[{}],
        )
        body = bg.pr_body()
        assert "ruff-c408" in body
        assert "src/main.py" in body

    def test_pr_body_cross_rule(self):
        f1 = _engine_finding(rule="ruff-c408")
        f2 = _engine_finding(finding_id="fid-2", rule="xo-no-any", line=50)
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="mixed",
            group_by="cross-rule",
            findings=[f1, f2],
            issues=[{}, {}],
        )
        body = bg.pr_body()
        assert "Specks by Rule" in body

    def test_pr_body_batch_with_issues(self):
        f1 = _engine_finding()
        f2 = _engine_finding(finding_id="fid-2", line=50)
        issues = [
            {"finding_id": "fid-1", "github": {"issue_number": 10}},
            {"finding_id": "fid-2", "github": {"issue_number": 11}},
        ]
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="ruff-c408",
            group_by="rule",
            findings=[f1, f2],
            issues=issues,
        )
        body = bg.pr_body()
        assert "#10" in body
        assert "#11" in body

    def test_from_solo(self):
        f = _engine_finding()
        issue = {"finding_id": "fid-1"}
        bg = BatchGroup.from_solo(issue, f)
        assert bg.batch_id.startswith("solo-")
        assert bg.is_solo

    def test_from_findings(self):
        f1 = _engine_finding()
        f2 = _engine_finding(finding_id="fid-2", line=50)
        issues_map = {"fid-1": {"id": "i1"}, "fid-2": {"id": "i2"}}
        config = BatchRule(rule_pattern="ruff-c408")
        bg = BatchGroup.from_findings([f1, f2], issues_map, config)
        assert bg.batch_id.startswith("batch-")
        assert len(bg.findings) == 2
        assert bg.max_files == 15

    def test_to_record(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="ruff-c408",
            group_by="solo",
            findings=[f],
            issues=[{"issue_id": "i1"}],
            status="open",
            branch="fix-branch",
            pr_number=42,
            pr_url="https://github.com/pr/42",
        )
        record = bg.to_record()
        assert record["batch_id"] == "b1"
        assert record["status"] == "open"
        assert record["branch"] == "fix-branch"
        assert record["pr_number"] == 42
        assert len(record["findings"]) == 1

    def test_to_record_with_fix_results_as_dict(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="solo",
            findings=[f],
            issues=[{}],
            fix_results={"fid-1": {"status": "success", "diff_lines": 5}},
        )
        record = bg.to_record()
        assert record["findings"][0]["fix_status"] == "success"

    def test_to_record_with_fix_results_as_object(self):
        f = _engine_finding()
        fr = FixResult(finding_id="fid-1", status="success", diff_lines=5)
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="solo",
            findings=[f],
            issues=[{}],
            fix_results={"fid-1": fr},
        )
        record = bg.to_record()
        assert record["findings"][0]["fix_status"] == "pending"

    def test_to_record_with_dict_fix_results(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="solo",
            findings=[f],
            issues=[{}],
            fix_results={"fid-1": {"status": "failed"}},
        )
        record = bg.to_record()
        assert record["findings"][0]["fix_status"] == "failed"

    def test_to_record_worktree_path(self):
        from pathlib import Path as _Path

        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1",
            rule_pattern="r",
            group_by="solo",
            findings=[f],
            issues=[{}],
            worktree_path=_Path("/tmp/worktree"),
        )
        record = bg.to_record()
        assert record["worktree_path"] == "/tmp/worktree"

    def test_to_record_none_worktree(self):
        f = _engine_finding()
        bg = BatchGroup(
            batch_id="b1", rule_pattern="r", group_by="solo", findings=[f], issues=[{}]
        )
        record = bg.to_record()
        assert record["worktree_path"] is None


class TestEngineHelperFunctions:
    def test_now_iso_returns_utc(self):
        result = engine_now_iso()
        assert "+00:00" in result or "Z" in result

    def test_parse_iso_valid(self):
        from datetime import datetime, timezone

        result = parse_iso("2026-01-15T10:30:00+00:00")
        assert result is not None
        assert result.year == 2026

    def test_parse_iso_z_suffix(self):
        result = parse_iso("2026-01-15T10:30:00Z")
        assert result is not None

    def test_parse_iso_none(self):
        assert parse_iso(None) is None

    def test_parse_iso_empty(self):
        assert parse_iso("") is None

    def test_parse_iso_invalid(self):
        assert parse_iso("not-a-date") is None

    def test_age_seconds(self):
        from datetime import datetime, timezone, timedelta

        past = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        result = age_seconds(past)
        assert result is not None
        assert 95 <= result <= 105

    def test_age_seconds_none(self):
        assert age_seconds(None) is None

    def test_age_seconds_invalid(self):
        assert age_seconds("not-a-date") is None

    def test_age_seconds_with_reference(self):
        from datetime import datetime, timezone, timedelta

        ref = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts = "2026-06-01T11:00:00+00:00"
        assert age_seconds(ts, reference=ref) == 3600

    def test_stable_finding_id_deterministic(self):
        id1 = stable_finding_id("repo", "path", 1, "rule", "snippet")
        id2 = stable_finding_id("repo", "path", 1, "rule", "snippet")
        assert id1 == id2

    def test_stable_finding_id_different_for_different_inputs(self):
        id1 = stable_finding_id("repo", "path", 1, "rule", "snippet")
        id2 = stable_finding_id("repo", "path", 2, "rule", "snippet")
        assert id1 != id2
