import json
import subprocess
import sys
from pathlib import Path

import pytest

from bluei.app.dashboard import (
    build_dashboard_data,
    render_dashboard_html,
    write_dashboard,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_build_dashboard_data_summarizes_repo_observability(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    (repos_dir / "demo" / "config.yaml").write_text(
        "name: demo\npath: /tmp/demo\nlanguage: python\n",
        encoding="utf-8",
    )
    _write_jsonl(
        state_dir / "health_trend.jsonl",
        [
            {
                "timestamp": "2026-05-17T00:00:00+00:00",
                "health": 70,
                "components": {"tests": 20, "lint": 30},
            },
            {
                "timestamp": "2026-05-18T00:00:00+00:00",
                "health": 82,
                "components": {"tests": 30, "lint": 35},
            },
        ],
    )
    _write_jsonl(
        state_dir / "fix_patterns.jsonl",
        [
            {
                "pattern_id": "fp-1",
                "rule": "broad-except",
                "confidence": 0.9,
                "success_count": 4,
            },
            {
                "pattern_id": "fp-2",
                "rule": "typed-dict",
                "confidence": 0.7,
                "success_count": 2,
            },
        ],
    )
    (state_dir / "emergent_rules.json").write_text(
        json.dumps(
            {
                "rules": [
                    {"rule_id": "er-1", "status": "proposed"},
                    {
                        "rule_id": "er-2",
                        "status": "active",
                        "false_positive_rate": 0.1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    campaign_dir = state_dir / "campaigns" / "camp-1"
    campaign_dir.mkdir(parents=True)
    (campaign_dir / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "camp-1",
                "repo": "demo",
                "title": "Type cleanup",
                "description": "",
                "strategy": "rule_based",
                "target_rules": ["typed-dict"],
                "target_paths": ["src/**"],
                "target_findings": ["f-1", "f-2"],
                "finding_snapshots": {},
                "phases": [
                    {
                        "phase_id": "p1",
                        "index": 0,
                        "title": "Phase",
                        "finding_ids": ["f-1"],
                    }
                ],
                "dependency_graph": {},
                "status": "running",
                "total_findings": 2,
                "findings_fixed": 1,
            }
        ),
        encoding="utf-8",
    )

    data = build_dashboard_data(repos_dir=repos_dir)

    assert data["repo_count"] == 1
    repo = data["repos"][0]
    assert repo["name"] == "demo"
    assert repo["language"] == "python"
    assert repo["current_vitality"] == 82
    assert repo["trend"] == 12
    assert repo["fix_patterns"]["active_count"] == 2
    assert repo["fix_patterns"]["top"][0]["rule"] == "broad-except"
    assert repo["emergent_rules"]["counts"] == {"active": 1, "proposed": 1}
    assert repo["campaigns"]["active_count"] == 1
    assert repo["campaigns"]["items"][0]["progress"] == 0.5


def test_build_dashboard_data_includes_operational_telemetry_and_raw_state(
    tmp_path,
) -> None:
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / "demo" / "state"
    state_dir.mkdir(parents=True)
    _write_jsonl(
        state_dir / "review_stats.jsonl",
        [
            {"findings_detected": 5, "findings_published": 3, "retry_failures": 1},
            {"findings_detected": 7, "findings_published": 4, "retry_failures": 2},
        ],
    )
    (state_dir / "auto_tune.json").write_text(
        json.dumps(
            {
                "mode": "active",
                "suggested_overrides": {"max_parallel": 2},
                "last_success_at": "2026-05-18T01:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "cycle_signals.json").write_text(
        json.dumps(
            {
                "suppressed_rules": {
                    "broad-except": {"until_cycle": 12},
                    "typed-dict": {"until_cycle": 14},
                }
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        state_dir / "rebase_stats.jsonl",
        [
            {"rebases_attempted": 2, "rebases_succeeded": 1, "rebases_failed": 1},
            {"rebases_attempted": 3, "rebases_succeeded": 3, "rebases_failed": 0},
        ],
    )

    data = build_dashboard_data(repos_dir=repos_dir)

    repo = data["repos"][0]
    assert repo["review_metrics"] == {
        "runs": 2,
        "findings_detected": 12,
        "findings_published": 7,
        "retry_failures": 3,
        "publication_rate": 7 / 12,
    }
    assert repo["auto_tune"]["mode"] == "active"
    assert repo["auto_tune"]["override_count"] == 1
    assert repo["cycle_signals"]["suppressed_count"] == 2
    assert repo["rebase_stats"]["attempted"] == 5
    assert repo["rebase_stats"]["succeeded"] == 4
    assert repo["rebase_stats"]["success_rate"] == 4 / 5
    assert "review_stats.jsonl" in repo["raw_state"]["files"]
    assert repo["raw_state"]["files"]["review_stats.jsonl"]["entries"] == 2


def test_render_dashboard_html_contains_key_sections(tmp_path) -> None:
    data = {
        "generated_at": "2026-05-18T00:00:00+00:00",
        "repo_count": 1,
        "repos": [
            {
                "name": "demo",
                "language": "python",
                "current_vitality": 82,
                "trend": 12,
                "last_run_at": "2026-05-18T00:00:00+00:00",
                "open_findings": 3,
                "health_history": [
                    {"timestamp": "t1", "health": 70},
                    {"timestamp": "t2", "health": 82},
                ],
                "fix_patterns": {
                    "active_count": 2,
                    "top": [
                        {"rule": "broad-except", "confidence": 0.9, "success_count": 4}
                    ],
                },
                "emergent_rules": {
                    "counts": {"active": 1},
                    "top_false_positive_rate": 0.1,
                },
                "campaigns": {
                    "active_count": 1,
                    "items": [
                        {
                            "campaign_id": "camp-1",
                            "title": "Type cleanup",
                            "status": "running",
                            "progress": 0.5,
                        }
                    ],
                },
                "escalations": {"recent": []},
                "review_metrics": {
                    "runs": 2,
                    "findings_detected": 12,
                    "findings_published": 7,
                    "retry_failures": 3,
                    "publication_rate": 7 / 12,
                },
                "auto_tune": {
                    "mode": "active",
                    "override_count": 1,
                    "suggested_overrides": {"max_parallel": 2},
                },
                "cycle_signals": {
                    "suppressed_count": 2,
                    "suppressed_rules": ["broad-except", "typed-dict"],
                },
                "rebase_stats": {"attempted": 5, "succeeded": 4, "success_rate": 4 / 5},
                "raw_state": {
                    "files": {"review_stats.jsonl": {"entries": 2, "bytes": 100}}
                },
            }
        ],
    }

    html = render_dashboard_html(data)

    assert "<title>bluei dashboard</title>" in html
    assert "Fleet Overview" in html
    assert "Learning Systems" in html
    assert "Campaign Tracker" in html
    assert "Review Cycle Health" in html
    assert "Raw State Explorer" in html
    assert "broad-except" in html


def test_write_dashboard_outputs_json_or_html(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    (repos_dir / "demo" / "state").mkdir(parents=True)

    json_path = write_dashboard(
        repos_dir=repos_dir,
        output_path=tmp_path / "dashboard.json",
        output_format="json",
    )
    html_path = write_dashboard(
        repos_dir=repos_dir,
        output_path=tmp_path / "dashboard.html",
        output_format="html",
    )

    assert json.loads(json_path.read_text())["repo_count"] == 1
    assert "bluei dashboard" in html_path.read_text()


def test_bluei_dashboard_cli_writes_json(tmp_path) -> None:
    repos_dir = tmp_path / "repos"
    (repos_dir / "demo" / "state").mkdir(parents=True)
    root = Path(__file__).resolve().parent.parent
    output = tmp_path / "dashboard.json"

    result = subprocess.run(
        [
            sys.executable,
            str(Path("bin/bluei.py")),
            "dashboard",
            "--state-root",
            str(repos_dir),
            "--output",
            str(output),
            "--format",
            "json",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Dashboard written:" in result.stdout
    assert json.loads(output.read_text())["repo_count"] == 1


# ---------------------------------------------------------------------------
# Tests extracted from test_phase6_modules_remaining.py
# ---------------------------------------------------------------------------

import re
from collections import Counter

from bluei.app.dashboard import (
    _build_repo_summary,
    _fmt,
    _health_band,
    _number,
    _percent,
    _rate,
    _read_json,
    _read_yaml,
    _render_campaigns,
    _render_escalation_feed,
    _render_flywheel_card,
    _render_notification_feed,
    _render_raw_state_card,
    _render_repo_card,
    _render_repo_row,
    _render_sparkline,
    _render_vitality_table,
    _repo_dirs,
    _scan_for_secrets,
    _severity_badge,
    _summarize_auto_tune,
    _summarize_campaigns,
    _summarize_cycle_signals,
    _summarize_emergent_rules,
    _summarize_fix_patterns,
    _summarize_notifications,
    _summarize_raw_state,
    _summarize_rebase_stats,
    _summarize_review_metrics,
)
from bluei.engine.jsonl import read_jsonl


class TestDashboardHelpers:
    """Tests for low-level dashboard helper functions."""

    def test_number_with_valid_value(self):
        assert _number(42, default=0) == 42.0
        assert _number("3.14", default=0) == 3.14

    def test_number_with_invalid_value(self):
        assert _number(None, default=5) == 5
        assert _number("abc", default=5) == 5
        assert _number([], default=10) == 10

    def test_fmt_positive(self):
        assert _fmt(42) == "42"
        assert _fmt(42, signed=True) == "+42"

    def test_fmt_zero_signed(self):
        assert _fmt(0, signed=True) == "0"

    def test_fmt_negative(self):
        assert _fmt(-5) == "-5"
        assert _fmt(-5, signed=True) == "-5"

    def test_percent(self):
        assert _percent(0.5) == "50%"
        assert _percent(1.0) == "100%"
        assert _percent(0) == "0%"

    def test_health_band_levels(self):
        assert _health_band(95) == "band-excellent"
        assert _health_band(90) == "band-excellent"
        assert _health_band(80) == "band-good"
        assert _health_band(75) == "band-good"
        assert _health_band(60) == "band-needs-work"
        assert _health_band(50) == "band-needs-work"
        assert _health_band(35) == "band-poor"
        assert _health_band(25) == "band-poor"
        assert _health_band(10) == "band-critical"
        assert _health_band(0) == "band-critical"


class TestReaders:
    """Tests for read_jsonl (from bluei.engine.jsonl), _read_json, _read_yaml."""

    def test_read_jsonl_missing_file(self, tmp_path):
        assert read_jsonl(tmp_path / "missing.jsonl", dicts_only=True) == []

    def test_read_jsonl_valid(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a":1}\n{"b":2}\n')
        result = read_jsonl(p, dicts_only=True)
        assert len(result) == 2
        assert result[0] == {"a": 1}

    def test_read_jsonl_skips_blank_and_invalid(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a":1}\n\nnot-json\n{"b":2}\n')
        result = read_jsonl(p, dicts_only=True)
        assert len(result) == 2

    def test_read_jsonl_skips_non_dict(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('[1,2,3]\n{"ok": true}\n')
        result = read_jsonl(p, dicts_only=True)
        assert len(result) == 1

    def test_read_json_missing_file(self, tmp_path):
        assert _read_json(tmp_path / "missing.json") == {}

    def test_read_json_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json")
        assert _read_json(p) == {}

    def test_read_json_non_dict(self, tmp_path):
        p = tmp_path / "list.json"
        p.write_text("[1,2,3]")
        assert _read_json(p) == {}

    def test_read_json_valid(self, tmp_path):
        p = tmp_path / "ok.json"
        p.write_text('{"key": "value"}')
        assert _read_json(p) == {"key": "value"}

    def test_read_yaml_missing_file(self, tmp_path):
        assert _read_yaml(tmp_path / "missing.yaml") == {}

    def test_read_yaml_invalid(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("{{invalid")
        assert _read_yaml(p) == {}

    def test_read_yaml_valid(self, tmp_path):
        p = tmp_path / "ok.yaml"
        p.write_text("name: test\nlanguage: python\n")
        assert _read_yaml(p) == {"name": "test", "language": "python"}


class TestRepoDirs:
    """Tests for _repo_dirs."""

    def test_specific_repo_exists(self, tmp_path):
        d = tmp_path / "repos" / "myrepo"
        d.mkdir(parents=True)
        result = _repo_dirs(tmp_path / "repos", "myrepo")
        assert len(result) == 1
        assert result[0].name == "myrepo"

    def test_specific_repo_missing(self, tmp_path):
        result = _repo_dirs(tmp_path / "repos", "nonexistent")
        assert result == []

    def test_all_repos(self, tmp_path):
        repos = tmp_path / "repos"
        (repos / "a").mkdir(parents=True)
        (repos / "b").mkdir(parents=True)
        (repos / "c.txt").touch()
        result = _repo_dirs(repos, None)
        names = {p.name for p in result}
        assert names == {"a", "b"}


class TestScanForSecrets:
    """Tests for _scan_for_secrets."""

    def test_redacts_ghp_tokens(self):
        text = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"'
        result = _scan_for_secrets(text)
        assert "ghp_" not in result
        assert "[REDACTED]" in result

    def test_redacts_sk_tokens(self):
        text = "sk_" + "A" * 48
        assert "sk_" not in _scan_for_secrets(text)

    def test_redacts_aws_keys(self):
        text = "AKIAIOSFODNN7EXAMPLE"
        assert "AKIA" not in _scan_for_secrets(text)

    def test_redacts_slack_tokens(self):
        text = "xoxb-1234-abcd-efgh"
        assert "xoxb-" not in _scan_for_secrets(text)
        text2 = "xoxp-1234-abcd"
        assert "xoxp-" not in _scan_for_secrets(text2)

    def test_preserves_clean_text(self):
        text = "no secrets here"
        assert _scan_for_secrets(text) == "no secrets here"


class TestSummarizeFixPatterns:
    def test_empty_file(self, tmp_path):
        result = _summarize_fix_patterns(tmp_path / "missing.jsonl")
        assert result["active_count"] == 0
        assert result["top"] == []

    def test_filters_low_confidence(self, tmp_path):
        p = tmp_path / "fix_patterns.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "pattern_id": "a",
                    "rule": "good",
                    "confidence": 0.9,
                    "success_count": 5,
                },
                {
                    "pattern_id": "b",
                    "rule": "low",
                    "confidence": 0.1,
                    "success_count": 1,
                },
            ],
        )
        result = _summarize_fix_patterns(p)
        assert result["active_count"] == 1
        assert result["top"][0]["pattern_id"] == "a"

    def test_top_10_limit(self, tmp_path):
        p = tmp_path / "fix_patterns.jsonl"
        rows = [
            {
                "pattern_id": f"p-{i}",
                "rule": f"r-{i}",
                "confidence": 0.5,
                "success_count": i,
            }
            for i in range(15)
        ]
        _write_jsonl(p, rows)
        result = _summarize_fix_patterns(p)
        assert result["active_count"] == 15
        assert len(result["top"]) == 10

    def test_sorts_by_success_then_confidence(self, tmp_path):
        p = tmp_path / "fix_patterns.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "pattern_id": "a",
                    "rule": "low-success",
                    "confidence": 0.95,
                    "success_count": 1,
                },
                {
                    "pattern_id": "b",
                    "rule": "high-success",
                    "confidence": 0.5,
                    "success_count": 10,
                },
            ],
        )
        result = _summarize_fix_patterns(p)
        assert result["top"][0]["pattern_id"] == "b"


class TestSummarizeEmergentRules:
    def test_empty_file(self, tmp_path):
        result = _summarize_emergent_rules(tmp_path / "missing.json")
        assert result["counts"] == {}
        assert result["top_false_positive_rate"] == 0

    def test_counts_status(self, tmp_path):
        p = tmp_path / "emergent_rules.json"
        p.write_text(
            json.dumps(
                {
                    "rules": [
                        {"status": "active"},
                        {"status": "active"},
                        {"status": "rejected"},
                    ]
                }
            )
        )
        result = _summarize_emergent_rules(p)
        assert result["counts"]["active"] == 2
        assert result["counts"]["rejected"] == 1

    def test_false_positive_rate(self, tmp_path):
        p = tmp_path / "emergent_rules.json"
        p.write_text(
            json.dumps(
                {
                    "rules": [
                        {"false_positive_rate": 0.1},
                        {"false_positive_rate": 0.5},
                        {"false_positive_rate": 0.3},
                    ]
                }
            )
        )
        result = _summarize_emergent_rules(p)
        assert result["top_false_positive_rate"] == 0.5


class TestSummarizeCampaigns:
    def test_missing_dir(self, tmp_path):
        result = _summarize_campaigns(tmp_path / "missing")
        assert result["active_count"] == 0
        assert result["items"] == []

    def test_active_and_completed(self, tmp_path):
        cdir = tmp_path / "campaigns"
        for name, status in [("running", "running"), ("done", "completed")]:
            d = cdir / name
            d.mkdir(parents=True)
            (d / "campaign.json").write_text(
                json.dumps(
                    {
                        "campaign_id": name,
                        "title": name,
                        "status": status,
                        "total_findings": 10,
                        "findings_fixed": 5,
                    }
                )
            )
        result = _summarize_campaigns(cdir)
        assert result["active_count"] == 1
        assert len(result["items"]) == 2

    def test_progress_calculation(self, tmp_path):
        campaigns_root = tmp_path / "campaigns"
        cdir = campaigns_root / "p1"
        cdir.mkdir(parents=True)
        (cdir / "campaign.json").write_text(
            json.dumps({"total_findings": 4, "findings_fixed": 1, "status": "running"})
        )
        result = _summarize_campaigns(campaigns_root)
        assert result["items"][0]["progress"] == 0.25

    def test_target_findings_fallback(self, tmp_path):
        campaigns_root = tmp_path / "campaigns"
        cdir = campaigns_root / "p2"
        cdir.mkdir(parents=True)
        (cdir / "campaign.json").write_text(
            json.dumps(
                {
                    "target_findings": ["a", "b", "c"],
                    "findings_fixed": 0,
                    "status": "running",
                }
            )
        )
        result = _summarize_campaigns(campaigns_root)
        assert result["items"][0]["total_findings"] == 3


class TestSummarizeReviewMetrics:
    def test_empty(self, tmp_path):
        result = _summarize_review_metrics(tmp_path / "missing.jsonl")
        assert result["runs"] == 0
        assert result["publication_rate"] == 0

    def test_aggregation(self, tmp_path):
        p = tmp_path / "review_stats.jsonl"
        _write_jsonl(
            p,
            [
                {"findings_detected": 10, "findings_published": 8, "retry_failures": 1},
                {"findings_detected": 5, "findings_published": 3, "retry_failures": 2},
            ],
        )
        result = _summarize_review_metrics(p)
        assert result["runs"] == 2
        assert result["findings_detected"] == 15
        assert result["findings_published"] == 11
        assert result["retry_failures"] == 3
        assert abs(result["publication_rate"] - 11 / 15) < 1e-9


class TestSummarizeAutoTune:
    def test_missing_file(self, tmp_path):
        result = _summarize_auto_tune(tmp_path / "missing.json")
        assert result["mode"] == "unknown"
        assert result["override_count"] == 0

    def test_with_overrides(self, tmp_path):
        p = tmp_path / "auto_tune.json"
        p.write_text(
            json.dumps(
                {
                    "mode": "active",
                    "suggested_overrides": {"a": 1, "b": 2},
                    "last_success_at": "2026-01-01",
                }
            )
        )
        result = _summarize_auto_tune(p)
        assert result["mode"] == "active"
        assert result["override_count"] == 2


class TestSummarizeCycleSignals:
    def test_missing_file(self, tmp_path):
        result = _summarize_cycle_signals(tmp_path / "missing.json")
        assert result["suppressed_count"] == 0

    def test_dict_suppressed_rules(self, tmp_path):
        p = tmp_path / "cycle_signals.json"
        p.write_text(
            json.dumps(
                {"suppressed_rules": {"rule-a": {"until": 5}, "rule-b": {"until": 3}}}
            )
        )
        result = _summarize_cycle_signals(p)
        assert result["suppressed_count"] == 2

    def test_list_suppressions(self, tmp_path):
        p = tmp_path / "cycle_signals.json"
        p.write_text(json.dumps({"suppressions": [{"rule": "x"}, {"rule": "y"}]}))
        result = _summarize_cycle_signals(p)
        assert result["suppressed_count"] == 2

    def test_filters_empty_rules(self, tmp_path):
        p = tmp_path / "cycle_signals.json"
        p.write_text(json.dumps({"suppressions": [{"rule": ""}, {"rule": "valid"}]}))
        result = _summarize_cycle_signals(p)
        assert result["suppressed_count"] == 1


class TestSummarizeRebaseStats:
    def test_empty(self, tmp_path):
        result = _summarize_rebase_stats(tmp_path / "missing.jsonl")
        assert result["attempted"] == 0
        assert result["success_rate"] == 0

    def test_aggregation(self, tmp_path):
        p = tmp_path / "rebase_stats.jsonl"
        _write_jsonl(
            p, [{"rebases_attempted": 3, "rebases_succeeded": 2, "rebases_failed": 1}]
        )
        result = _summarize_rebase_stats(p)
        assert result["attempted"] == 3
        assert result["succeeded"] == 2
        assert result["failed"] == 1
        assert abs(result["success_rate"] - 2 / 3) < 1e-9


class TestSummarizeNotifications:
    def test_empty(self, tmp_path):
        result = _summarize_notifications(tmp_path / "missing.jsonl")
        assert result["total"] == 0
        assert result["channels"] == []
        assert result["last_delivery_at"] is None

    def test_with_deliveries(self, tmp_path):
        p = tmp_path / "notif.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "timestamp": "2026-01-01T00:00:00",
                    "deliveries": [
                        {"channel_type": "slack", "success": True},
                        {"channel_type": "log", "success": True},
                    ],
                },
                {
                    "timestamp": "2026-01-02T00:00:00",
                    "deliveries": [{"channel_type": "email", "success": False}],
                },
            ],
        )
        result = _summarize_notifications(p)
        assert result["total"] == 2
        assert result["delivered"] == 1
        assert result["failed"] == 1
        assert "slack" in result["channels"]
        assert "email" in result["channels"]


class TestSummarizeRawState:
    def test_missing_dir(self, tmp_path):
        result = _summarize_raw_state(tmp_path / "missing")
        assert result["files"] == {}

    def test_counts_jsonl_entries(self, tmp_path):
        d = tmp_path / "state"
        d.mkdir()
        p = d / "data.jsonl"
        _write_jsonl(p, [{"a": 1}, {"a": 2}])
        result = _summarize_raw_state(d)
        assert result["files"]["data.jsonl"]["entries"] == 2

    def test_counts_json_entries(self, tmp_path):
        d = tmp_path / "state"
        d.mkdir()
        (d / "config.json").write_text('{"x": 1}')
        result = _summarize_raw_state(d)
        assert result["files"]["config.json"]["entries"] == 1


class TestBuildRepoSummary:
    def test_full_summary(self, tmp_path):
        repo_dir = tmp_path / "repos" / "myrepo"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: rust\n")
        _write_jsonl(
            state_dir / "health_trend.jsonl",
            [
                {"timestamp": "t1", "health": 60},
                {"timestamp": "t2", "health": 80},
            ],
        )
        _write_jsonl(state_dir / "findings.jsonl", [{"id": 1}, {"id": 2}])
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["name"] == "myrepo"
        assert result["language"] == "rust"
        assert result["current_vitality"] == 80
        assert result["trend"] == 20
        assert result["open_findings"] == 2

    def test_empty_health_history(self, tmp_path):
        repo_dir = tmp_path / "repos" / "empty"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["current_vitality"] == 0
        assert result["trend"] == 0
        assert result["last_run_at"] is None

    def test_history_limit(self, tmp_path):
        repo_dir = tmp_path / "repos" / "limited"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        entries = [{"timestamp": f"t{i}", "health": i} for i in range(50)]
        _write_jsonl(state_dir / "health_trend.jsonl", entries)
        result = _build_repo_summary(repo_dir, history_limit=10)
        assert len(result["health_history"]) == 10

    def test_uses_score_key_fallback(self, tmp_path):
        repo_dir = tmp_path / "repos" / "score-key"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: go\n")
        _write_jsonl(
            state_dir / "health_trend.jsonl",
            [
                {"timestamp": "t1", "score": 40},
                {"timestamp": "t2", "score": 70},
            ],
        )
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["current_vitality"] == 70
        assert result["trend"] == 30

    def test_no_config_yaml(self, tmp_path):
        repo_dir = tmp_path / "repos" / "noconfig"
        (repo_dir / "state").mkdir(parents=True)
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["language"] == "unknown"


class TestBuildDashboardDataExtra:
    def test_single_repo(self, tmp_path):
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "demo"
        (repo_dir / "state").mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        data = build_dashboard_data(repos_dir=repos_dir, repo_name="demo")
        assert data["repo_count"] == 1

    def test_specific_repo_missing(self, tmp_path):
        repos_dir = tmp_path / "repos"
        data = build_dashboard_data(repos_dir=repos_dir, repo_name="nonexistent")
        assert data["repo_count"] == 0

    def test_sorted_by_name(self, tmp_path):
        repos_dir = tmp_path / "repos"
        for name in ["z-repo", "a-repo", "m-repo"]:
            d = repos_dir / name / "state"
            d.mkdir(parents=True)
            (repos_dir / name / "config.yaml").write_text("language: python\n")
        data = build_dashboard_data(repos_dir=repos_dir)
        names = [r["name"] for r in data["repos"]]
        assert names == ["a-repo", "m-repo", "z-repo"]


class TestWriteDashboardExtra:
    def test_json_output(self, tmp_path):
        repos_dir = tmp_path / "repos"
        (repos_dir / "demo" / "state").mkdir(parents=True)
        out = tmp_path / "dash.json"
        result = write_dashboard(
            repos_dir=repos_dir, output_path=out, output_format="json"
        )
        data = json.loads(result.read_text())
        assert "generated_at" in data

    def test_html_output(self, tmp_path):
        repos_dir = tmp_path / "repos"
        (repos_dir / "demo" / "state").mkdir(parents=True)
        out = tmp_path / "dash.html"
        result = write_dashboard(
            repos_dir=repos_dir, output_path=out, output_format="html"
        )
        content = result.read_text()
        assert "bluei dashboard" in content

    def test_invalid_format_raises(self, tmp_path):
        repos_dir = tmp_path / "repos"
        (repos_dir / "demo" / "state").mkdir(parents=True)
        with pytest.raises(ValueError, match="unsupported dashboard format"):
            write_dashboard(
                repos_dir=repos_dir, output_path=tmp_path / "x.csv", output_format="csv"
            )

    def test_creates_parent_dirs(self, tmp_path):
        repos_dir = tmp_path / "repos"
        (repos_dir / "demo" / "state").mkdir(parents=True)
        out = tmp_path / "sub" / "dir" / "dashboard.json"
        result = write_dashboard(
            repos_dir=repos_dir, output_path=out, output_format="json"
        )
        assert result.exists()

    def test_html_secret_redaction(self, tmp_path):
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "demo"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        _write_jsonl(
            state_dir / "findings.jsonl",
            [
                {
                    "id": "f1",
                    "snippet": "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn",
                },
            ],
        )
        out = tmp_path / "dash.html"
        write_dashboard(repos_dir=repos_dir, output_path=out, output_format="html")
        content = out.read_text()
        assert "ghp_" not in content

    def test_html_size_cap_truncation(self, tmp_path):
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "big"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        big_history = [
            {"timestamp": f"t{i}", "health": 50, "data": "x" * 50000}
            for i in range(200)
        ]
        _write_jsonl(state_dir / "health_trend.jsonl", big_history)
        out = tmp_path / "big.html"
        write_dashboard(repos_dir=repos_dir, output_path=out, output_format="html")
        assert out.exists()


class TestRenderFunctions:
    def test_render_repo_row(self):
        repo = {
            "name": "test-repo",
            "language": "python",
            "current_vitality": 85,
            "trend": -5,
            "open_findings": 3,
            "last_run_at": "2026-01-01",
        }
        html = _render_repo_row(repo)
        assert "test-repo" in html
        assert "down" in html
        assert "85" in html

    def test_render_repo_row_positive_trend(self):
        html = _render_repo_row({"name": "x", "trend": 10, "current_vitality": 90})
        assert "up" in html

    def test_render_repo_row_no_last_run(self):
        html = _render_repo_row(
            {"name": "x", "trend": 0, "current_vitality": 50, "last_run_at": None}
        )
        assert "never" in html

    def test_render_repo_card(self):
        repo = {
            "name": "demo",
            "fix_patterns": {"active_count": 5, "top": [{"rule": "my-rule"}]},
            "emergent_rules": {"counts": {"active": 3}},
            "health_history": [
                {"timestamp": "t1", "health": 80},
                {"timestamp": "t2", "health": 90},
            ],
        }
        html = _render_repo_card(repo)
        assert "demo" in html
        assert "my-rule" in html

    def test_render_campaigns_empty(self):
        repo = {"name": "x", "campaigns": {"items": []}}
        html = _render_campaigns(repo)
        assert "No campaigns" in html

    def test_render_campaigns_with_items(self):
        repo = {
            "name": "x",
            "campaigns": {
                "items": [
                    {
                        "title": "Cleanup",
                        "campaign_id": "c1",
                        "status": "running",
                        "progress": 0.75,
                    }
                ]
            },
        }
        html = _render_campaigns(repo)
        assert "Cleanup" in html
        assert "75%" in html

    def test_render_escalation_feed_empty(self):
        html = _render_escalation_feed(
            {"repos": [{"name": "x", "escalations": {"recent": []}}]}
        )
        assert "No recent escalations" in html

    def test_render_escalation_feed_with_data(self):
        data = {
            "repos": [
                {
                    "name": "myrepo",
                    "escalations": {
                        "recent": [
                            {
                                "reason": "bad code",
                                "severity": "critical",
                                "timestamp": "t1",
                                "finding_id": "f1",
                            }
                        ]
                    },
                }
            ]
        }
        html = _render_escalation_feed(data)
        assert "myrepo" in html
        assert "bad code" in html

    def test_render_notification_feed_empty(self):
        html = _render_notification_feed(
            {"repos": [{"name": "x", "notifications": {"recent": []}}]}
        )
        assert "No notification deliveries" in html

    def test_render_notification_feed_with_data(self):
        data = {
            "repos": [
                {
                    "name": "r",
                    "notifications": {
                        "recent": [
                            {
                                "severity": "high",
                                "escalation_type": "test",
                                "timestamp": "2026-01-01T00:00:00Z",
                                "deliveries": [
                                    {"channel_type": "slack", "success": True}
                                ],
                            }
                        ]
                    },
                }
            ]
        }
        html = _render_notification_feed(data)
        assert "r" in html
        assert "slack:ok" in html

    def test_render_raw_state_card_with_files(self):
        repo = {
            "name": "x",
            "raw_state": {"files": {"data.jsonl": {"entries": 5, "bytes": 100}}},
        }
        html = _render_raw_state_card(repo)
        assert "data.jsonl" in html
        assert "5 entries" in html

    def test_render_sparkline_with_data(self):
        history = [{"health": 90}, {"health": 60}, {"health": 30}]
        html = _render_sparkline(history)
        assert "sparkline-dot" in html
        assert "sparkline-alert" in html

    def test_render_vitality_table_with_data(self):
        history = [
            {"timestamp": "2026-01-01T00:00:00", "health": 40},
            {"timestamp": "2026-01-02T00:00:00", "health": 80},
        ]
        html = _render_vitality_table(history)
        assert "Score" in html
        assert "Time" in html

    def test_severity_badge_critical(self):
        assert "band-critical" in _severity_badge("critical")

    def test_severity_badge_empty(self):
        assert "info" in _severity_badge("")

    def test_render_dashboard_full_html(self):
        data = {
            "generated_at": "2026-01-01",
            "repos": [
                {
                    "name": "x",
                    "language": "python",
                    "current_vitality": 80,
                    "trend": 5,
                    "last_run_at": "t1",
                    "open_findings": 2,
                    "health_history": [{"timestamp": "t1", "health": 80}],
                    "fix_patterns": {"active_count": 0, "top": []},
                    "emergent_rules": {"counts": {}},
                    "campaigns": {"items": []},
                    "escalations": {"recent": []},
                    "review_metrics": {
                        "runs": 0,
                        "publication_rate": 0,
                        "retry_failures": 0,
                    },
                    "auto_tune": {"mode": "off", "override_count": 0},
                    "cycle_signals": {"suppressed_count": 0},
                    "rebase_stats": {"success_rate": 0},
                    "notifications": {"recent": []},
                    "raw_state": {"files": {}},
                }
            ],
        }
        html_str = render_dashboard_html(data)
        assert "bluei dashboard" in html_str
        assert "Fleet Overview" in html_str


# ---------------------------------------------------------------------------
# Flywheel Ledger card (PROMPT-08 / T3.5)
# ---------------------------------------------------------------------------


def _flywheel_ledger_fixture() -> dict:
    """Fixture matching slice-5 finalize output shape."""
    return {
        "findings_attempted": 10,
        "resolved_deterministic_by_stage": {
            "pattern-replay": 3,
            "recipe": 2,
            "ast": 1,
        },
        "resolved_deterministic_total": 6,
        "resolved_llm": 3,
        "exhausted": 1,
        "pattern_replay_resolutions": 3,
        "savings_usd": 0.42,
        "cost_total_usd": 1.20,
        "rates": {
            "deterministic_resolution": "6/10 (60.0%)",
            "llm_fallback": "3/10 (30.0%)",
            "exhausted": "1/10 (10.0%)",
        },
        "active_pattern_count": 7,
        "top_failing_patterns": [
            {"pattern_id": "p-1", "rule": "broad-except", "failure_count": 5},
            {"pattern_id": "p-2", "rule": "<script>x</script>", "failure_count": 2},
        ],
    }


class TestRateHelper:
    def test_normal(self):
        assert _rate(6, 10) == "6/10 (60.0%)"

    def test_zero_denom(self):
        assert _rate(0, 0) == "n/a"

    def test_zero_num(self):
        assert _rate(0, 5) == "0/5 (0.0%)"


class TestBuildRepoSummaryFlywheel:
    def test_includes_flywheel_ledger_from_status_json(self, tmp_path):
        repo_dir = tmp_path / "repos" / "demo"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        ledger = _flywheel_ledger_fixture()
        (state_dir / "status.json").write_text(
            json.dumps({"flywheel_ledger": ledger}), encoding="utf-8"
        )
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["flywheel_ledger"] == ledger

    def test_empty_when_status_json_missing(self, tmp_path):
        repo_dir = tmp_path / "repos" / "demo"
        (repo_dir / "state").mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["flywheel_ledger"] == {}

    def test_empty_when_status_json_malformed(self, tmp_path):
        repo_dir = tmp_path / "repos" / "demo"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        (state_dir / "status.json").write_text("not valid json", encoding="utf-8")
        result = _build_repo_summary(repo_dir, history_limit=30)
        assert result["flywheel_ledger"] == {}


class TestRenderFlywheelCard:
    def test_empty_returns_empty_string(self):
        assert _render_flywheel_card({"name": "x", "flywheel_ledger": {}}) == ""

    def test_missing_returns_empty_string(self):
        assert _render_flywheel_card({"name": "x"}) == ""

    def test_renders_metrics_and_footnote(self):
        repo = {"name": "demo", "flywheel_ledger": _flywheel_ledger_fixture()}
        card = _render_flywheel_card(repo)
        assert "Deterministic Flywheel — demo" in card
        # ADR-0002 num/denom (%) rates
        assert "6/10 (60.0%)" in card
        assert "3/10 (30.0%)" in card
        assert "1/10 (10.0%)" in card
        # Savings (ADR-0003)
        assert "$0.42" in card
        # Per-stage table
        assert "pattern-replay" in card
        assert "recipe" in card
        assert "ast" in card
        # Pattern replay
        assert "3" in card  # replay hits
        assert "7" in card  # active patterns
        # Top failing pattern
        assert "broad-except" in card
        # ADR-0003 footnote verbatim
        assert "standalone Pattern-replay substitutions" in card

    def test_escapes_rule_names(self):
        ledger = _flywheel_ledger_fixture()
        repo = {"name": "demo", "flywheel_ledger": ledger}
        card = _render_flywheel_card(repo)
        assert "<script>" not in card
        assert "&lt;script&gt;" in card

    def test_divide_by_zero_na(self):
        ledger = {
            "findings_attempted": 0,
            "resolved_deterministic_total": 0,
            "resolved_llm": 0,
            "exhausted": 0,
            "savings_usd": 0.0,
            "cost_total_usd": 0.0,
            "resolved_deterministic_by_stage": {},
            "pattern_replay_resolutions": 0,
            "active_pattern_count": 0,
            "top_failing_patterns": [],
        }
        card = _render_flywheel_card({"name": "z", "flywheel_ledger": ledger})
        assert "n/a" in card


class TestDashboardFlywheelRendering:
    def test_render_includes_flywheel_card_in_learning(self):
        data = {
            "generated_at": "2026-01-01",
            "repos": [
                {
                    "name": "demo",
                    "language": "python",
                    "current_vitality": 80,
                    "trend": 5,
                    "last_run_at": "t1",
                    "open_findings": 0,
                    "health_history": [{"timestamp": "t1", "health": 80}],
                    "fix_patterns": {"active_count": 0, "top": []},
                    "emergent_rules": {"counts": {}},
                    "campaigns": {"items": []},
                    "escalations": {"recent": []},
                    "review_metrics": {
                        "runs": 0,
                        "publication_rate": 0,
                        "retry_failures": 0,
                    },
                    "auto_tune": {"mode": "off", "override_count": 0},
                    "cycle_signals": {"suppressed_count": 0},
                    "rebase_stats": {"success_rate": 0},
                    "notifications": {"recent": []},
                    "raw_state": {"files": {}},
                    "flywheel_ledger": _flywheel_ledger_fixture(),
                }
            ],
        }
        html_str = render_dashboard_html(data)
        assert "Deterministic Flywheel — demo" in html_str
        assert "6/10 (60.0%)" in html_str

    def test_render_omits_flywheel_section_when_empty(self):
        data = {
            "generated_at": "2026-01-01",
            "repos": [
                {
                    "name": "demo",
                    "language": "python",
                    "current_vitality": 80,
                    "trend": 5,
                    "last_run_at": "t1",
                    "open_findings": 0,
                    "health_history": [{"timestamp": "t1", "health": 80}],
                    "fix_patterns": {"active_count": 0, "top": []},
                    "emergent_rules": {"counts": {}},
                    "campaigns": {"items": []},
                    "escalations": {"recent": []},
                    "review_metrics": {
                        "runs": 0,
                        "publication_rate": 0,
                        "retry_failures": 0,
                    },
                    "auto_tune": {"mode": "off", "override_count": 0},
                    "cycle_signals": {"suppressed_count": 0},
                    "rebase_stats": {"success_rate": 0},
                    "notifications": {"recent": []},
                    "raw_state": {"files": {}},
                    "flywheel_ledger": {},
                }
            ],
        }
        html_str = render_dashboard_html(data)
        assert "Deterministic Flywheel" not in html_str
        # Existing Learning Systems layout intact
        assert "Learning Systems" in html_str

    def test_existing_cards_grid_intact(self):
        """Flywheel cards sit alongside the existing cards grid without breaking it."""
        data = {
            "generated_at": "2026-01-01",
            "repos": [
                {
                    "name": "demo",
                    "language": "python",
                    "current_vitality": 80,
                    "trend": 5,
                    "last_run_at": "t1",
                    "open_findings": 0,
                    "health_history": [{"timestamp": "t1", "health": 80}],
                    "fix_patterns": {"active_count": 1, "top": [{"rule": "my-rule"}]},
                    "emergent_rules": {"counts": {"active": 1}},
                    "campaigns": {"items": []},
                    "escalations": {"recent": []},
                    "review_metrics": {
                        "runs": 0,
                        "publication_rate": 0,
                        "retry_failures": 0,
                    },
                    "auto_tune": {"mode": "off", "override_count": 0},
                    "cycle_signals": {"suppressed_count": 0},
                    "rebase_stats": {"success_rate": 0},
                    "notifications": {"recent": []},
                    "raw_state": {"files": {}},
                    "flywheel_ledger": _flywheel_ledger_fixture(),
                }
            ],
        }
        html_str = render_dashboard_html(data)
        # Original card content still present
        assert "my-rule" in html_str
        # Flywheel card present
        assert "Deterministic Flywheel — demo" in html_str
        # Other tabs intact
        assert "Campaign Tracker" in html_str
        assert "Review Cycle Health" in html_str
        assert "Raw State Explorer" in html_str


class TestWriteDashboardFlywheelRoundTrip:
    def test_html_contains_section_for_populated_fixture(self, tmp_path):
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "demo"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        (state_dir / "status.json").write_text(
            json.dumps({"flywheel_ledger": _flywheel_ledger_fixture()}),
            encoding="utf-8",
        )
        out = tmp_path / "dash.html"
        write_dashboard(repos_dir=repos_dir, output_path=out, output_format="html")
        content = out.read_text()
        assert "Deterministic Flywheel — demo" in content
        assert "6/10 (60.0%)" in content

    def test_html_omits_section_for_empty(self, tmp_path):
        repos_dir = tmp_path / "repos"
        repo_dir = repos_dir / "demo"
        state_dir = repo_dir / "state"
        state_dir.mkdir(parents=True)
        (repo_dir / "config.yaml").write_text("language: python\n")
        out = tmp_path / "dash.html"
        write_dashboard(repos_dir=repos_dir, output_path=out, output_format="html")
        content = out.read_text()
        assert "Deterministic Flywheel" not in content
