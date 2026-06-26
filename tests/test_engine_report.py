import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.report import (
    _infer_category,
    _infer_language,
    infer_language_from_path,
    _normalize_category,
    _build_rule_catalog,
    _classify_findings,
    _build_findings_by_language,
    _build_top_rules,
    _build_health_trend,
    _collect_run_metrics,
    _load_reconciliation_events,
    extract_report_data,
    _to_template_format,
    _generate_placeholder_html,
    generate_report_html,
)


class TestInferCategory:
    def test_mdl_prefix(self):
        assert _infer_category("mdl-001") == "style"

    def test_ruff_prefix(self):
        assert _infer_category("ruff-c408") == "lint"

    def test_test_gap_prefix(self):
        assert _infer_category("test-gap-assert") == "test"

    def test_test_coverage_prefix(self):
        assert _infer_category("test-coverage-missing") == "test"

    def test_secret_prefix(self):
        assert _infer_category("secret-api-key") == "secret"

    def test_shellcheck_prefix(self):
        assert _infer_category("shellcheck-sc2086") == "lint"

    def test_hadolint_prefix(self):
        assert _infer_category("hadolint-dl3006") == "lint"

    def test_actionlint_prefix(self):
        assert _infer_category("actionlint-run-check") == "lint"

    def test_go_staticcheck_prefix(self):
        assert _infer_category("go-staticcheck-sa1000") == "lint"

    def test_go_unused_prefix(self):
        assert _infer_category("go-unused-var") == "dead-code"

    def test_debt_prefix(self):
        assert _infer_category("debt-old-api") == "debt"

    def test_todo_prefix(self):
        assert _infer_category("todo-refactor") == "debt"

    def test_doc_prefix(self):
        assert _infer_category("doc-missing") == "docs"

    def test_docs_prefix(self):
        assert _infer_category("docs-outdated") == "docs"

    def test_perf_prefix(self):
        assert _infer_category("perf-n-plus-1") == "performance"

    def test_type_prefix(self):
        assert _infer_category("type-mismatch") == "type-safety"

    def test_refactor_prefix(self):
        assert _infer_category("refactor-extract") == "refactor"

    def test_xo_complex_prefix(self):
        assert _infer_category("xo-complex-func") == "refactor"

    def test_xo_max_prefix(self):
        assert _infer_category("xo-max-lines") == "refactor"

    def test_xo_prefix(self):
        assert _infer_category("xo-no-any") == "lint"

    def test_domain_bug_prefixes(self):
        for rule in [
            "discount-bug",
            "orders-missing",
            "inventory-race",
            "notifications-stale",
            "catalog-null",
        ]:
            assert _infer_category(rule) == "bug"

    def test_broad_except(self):
        assert _infer_category("broad-except") == "lint"

    def test_hardcoded_prefix(self):
        assert _infer_category("hardcoded-creds") == "lint"

    def test_trailing_prefix(self):
        assert _infer_category("trailing-ws") == "lint"

    def test_fallback(self):
        assert _infer_category("unknown-rule") == "other"

    def test_custom_fallback(self):
        assert _infer_category("unknown-rule", fallback="custom") == "custom"


class TestInferLanguage:
    def test_mdl(self):
        assert _infer_language("mdl-001") == "markdown"

    def test_ruff(self):
        assert _infer_language("ruff-c408") == "python"

    def test_shellcheck(self):
        assert _infer_language("shellcheck-sc1") == "shell"

    def test_hadolint(self):
        assert _infer_language("hadolint-dl1") == "dockerfile"

    def test_actionlint(self):
        assert _infer_language("actionlint-x") == "github-actions"

    def test_go_staticcheck(self):
        assert _infer_language("go-staticcheck-sa1") == "go"

    def test_go_unused(self):
        assert _infer_language("go-unused-var") == "go"

    def test_secret(self):
        assert _infer_language("secret-key") == "generic"

    def test_test_gap(self):
        assert _infer_language("test-gap-missing") == "python"

    def test_xo(self):
        assert _infer_language("xo-no-any") == "typescript"

    def test_fallback(self):
        assert _infer_language("weird-rule") == "unknown"

    def test_custom_fallback(self):
        assert _infer_language("weird-rule", fallback="cobol") == "cobol"


class TestInferLanguageFromPath:
    """Tests for the path-suffix based language inference bridged from
    bluei.app.emergent_rules. This function takes a FILE PATH, whereas
    _infer_language takes a RULE NAME."""

    def test_python(self):
        assert infer_language_from_path("src/api/users.py") == "python"

    def test_typescript(self):
        assert infer_language_from_path("src/ui/app.ts") == "typescript"
        assert infer_language_from_path("src/ui/App.tsx") == "typescript"

    def test_javascript_suffix(self):
        # Aligned with codebase convention (48+ refs map .js/.jsx → javascript);
        # was "typescript" pre-2026-06-17.
        assert infer_language_from_path("src/legacy.js") == "javascript"
        assert infer_language_from_path("src/legacy.jsx") == "javascript"

    def test_go(self):
        assert infer_language_from_path("cmd/main.go") == "go"

    def test_rust(self):
        assert infer_language_from_path("src/lib.rs") == "rust"

    def test_unknown_suffix_defaults_to_all(self):
        assert infer_language_from_path("README.md") == "all"
        assert infer_language_from_path("Dockerfile") == "all"
        assert infer_language_from_path("config.yaml") == "all"

    def test_custom_fallback(self):
        assert infer_language_from_path("Dockerfile", fallback="other") == "other"

    def test_case_insensitive_suffix(self):
        assert infer_language_from_path("SRC/MAIN.PY") == "python"


class TestNormalizeCategory:
    def test_todo_debt(self):
        assert _normalize_category("todo/debt") == "debt"

    def test_docs_mismatch(self):
        assert _normalize_category("docs-mismatch") == "docs"

    def test_docs_gap(self):
        assert _normalize_category("docs-gap") == "docs"

    def test_docs_drift(self):
        assert _normalize_category("docs-drift") == "docs"

    def test_perf_smell(self):
        assert _normalize_category("perf-smell") == "performance"

    def test_test_gap(self):
        assert _normalize_category("test-gap") == "test"

    def test_test_coverage(self):
        assert _normalize_category("test-coverage") == "test"

    def test_type_safety(self):
        assert _normalize_category("type-safety") == "type-safety"

    def test_dead_code(self):
        assert _normalize_category("dead-code") == "refactor"

    def test_simplify(self):
        assert _normalize_category("simplify") == "refactor"

    def test_passthrough(self):
        assert _normalize_category("bug") == "bug"

    def test_uppercase(self):
        assert _normalize_category("BUG") == "bug"

    def test_spaces(self):
        assert _normalize_category("type safety") == "type-safety"

    def test_underscores(self):
        assert _normalize_category("type_safety") == "type-safety"


class TestBuildRuleCatalog:
    def test_extracts_rules_with_category_and_language(self):
        status = {
            "detector_catalog": [
                {"rule": "ruff-c408", "category": "lint", "language": "python"},
                {
                    "rule": "xo-no-any",
                    "category": "type-safety",
                    "language": "typescript",
                },
            ]
        }
        cat, lang = _build_rule_catalog(status)
        assert cat == {"ruff-c408": "lint", "xo-no-any": "type-safety"}
        assert lang == {"ruff-c408": "python", "xo-no-any": "typescript"}

    def test_empty_catalog(self):
        cat, lang = _build_rule_catalog({})
        assert cat == {}
        assert lang == {}

    def test_entry_without_rule_skipped(self):
        status = {"detector_catalog": [{"category": "lint"}]}
        cat, lang = _build_rule_catalog(status)
        assert cat == {}

    def test_entry_without_category_skipped_for_cat(self):
        status = {"detector_catalog": [{"rule": "ruff-x", "language": "python"}]}
        cat, lang = _build_rule_catalog(status)
        assert cat == {}
        assert lang == {"ruff-x": "python"}

    def test_category_normalized(self):
        status = {"detector_catalog": [{"rule": "r1", "category": "perf-smell"}]}
        cat, _ = _build_rule_catalog(status)
        assert cat["r1"] == "performance"


class TestClassifyFindings:
    def test_uses_catalog_when_available(self):
        findings = [{"rule": "ruff-c408"}, {"rule": "ruff-c408"}, {"rule": "xo-no-any"}]
        cat = {"ruff-c408": "lint", "xo-no-any": "type-safety"}
        result = _classify_findings(findings, cat)
        assert result["lint"] == 2
        assert result["type-safety"] == 1

    def test_infers_when_not_in_catalog(self):
        findings = [{"rule": "mdl-001"}, {"rule": "unknown-rule"}]
        result = _classify_findings(findings, {})
        assert result["style"] == 1
        assert result["other"] == 1

    def test_empty_findings(self):
        result = _classify_findings([], {})
        assert sum(result.values()) == 0

    def test_missing_rule_field(self):
        findings = [{}, {"rule": ""}]
        result = _classify_findings(findings, {})
        assert result["other"] == 2


class TestBuildFindingsByLanguage:
    def test_uses_catalog(self):
        findings = [{"rule": "ruff-c408"}, {"rule": "xo-no-any"}]
        lang = {"ruff-c408": "python", "xo-no-any": "typescript"}
        result = _build_findings_by_language(findings, lang)
        assert result["python"] == 1
        assert result["typescript"] == 1

    def test_infers_when_not_in_catalog(self):
        findings = [{"rule": "shellcheck-sc1"}, {"rule": "unknown"}]
        result = _build_findings_by_language(findings, {})
        assert result["shell"] == 1
        assert result["unknown"] == 1

    def test_empty(self):
        result = _build_findings_by_language([], {})
        assert sum(result.values()) == 0


class TestBuildTopRules:
    def test_top_rules_sorted_by_count(self):
        findings = [{"rule": "ruff-c408"}] * 5 + [{"rule": "xo-no-any"}] * 2
        result = _build_top_rules(findings, {})
        assert result[0]["rule"] == "ruff-c408"
        assert result[0]["count"] == 5
        assert result[1]["rule"] == "xo-no-any"

    def test_respects_limit(self):
        findings = [{"rule": f"rule-{i}"} for i in range(20)]
        result = _build_top_rules(findings, {}, limit=5)
        assert len(result) == 5

    def test_severity_high_for_security(self):
        findings = [{"rule": "secret-api-key"}]
        result = _build_top_rules(findings, {"secret-api-key": "secret"})
        assert result[0]["severity"] == "high"

    def test_severity_high_for_bug(self):
        findings = [{"rule": "discount-bug"}]
        result = _build_top_rules(findings, {"discount-bug": "bug"})
        assert result[0]["severity"] == "high"

    def test_severity_medium_for_performance(self):
        findings = [{"rule": "perf-x"}]
        result = _build_top_rules(findings, {"perf-x": "performance"})
        assert result[0]["severity"] == "medium"

    def test_severity_medium_for_test(self):
        findings = [{"rule": "test-gap-x"}]
        result = _build_top_rules(findings, {"test-gap-x": "test"})
        assert result[0]["severity"] == "medium"

    def test_severity_low_for_lint(self):
        findings = [{"rule": "ruff-x"}]
        result = _build_top_rules(findings, {"ruff-x": "lint"})
        assert result[0]["severity"] == "low"

    def test_skips_empty_rules(self):
        findings = [{"rule": ""}, {"rule": "ruff-x"}]
        result = _build_top_rules(findings, {})
        assert len(result) == 1

    def test_infers_category_when_not_in_catalog(self):
        findings = [{"rule": "mdl-001"}]
        result = _build_top_rules(findings, {})
        assert result[0]["category"] == "style"

    def test_default_limit_15(self):
        findings = [{"rule": f"r-{i}"} for i in range(20)]
        result = _build_top_rules(findings, {})
        assert len(result) == 15


class TestBuildHealthTrend:
    def test_builds_trend_from_history(self):
        history = [
            {"timestamp": "2026-01-01T00:00:00Z", "score": 75.0, "findings_count": 10},
            {"timestamp": "2026-01-02T00:00:00Z", "score": 80.0, "findings_count": 8},
        ]
        result = _build_health_trend(Path("/tmp"), history)
        assert len(result) == 2
        assert result[0]["date"] == "2026-01-01"
        assert result[0]["score"] == 75.0
        assert result[1]["findings_count"] == 8

    def test_empty_history(self):
        result = _build_health_trend(Path("/tmp"), [])
        assert result == []

    def test_limits_to_days(self):
        history = [
            {
                "timestamp": f"2026-01-{i:02d}T00:00:00Z",
                "score": float(i),
                "findings_count": i,
            }
            for i in range(1, 40)
        ]
        result = _build_health_trend(Path("/tmp"), history, days=5)
        assert len(result) == 5

    def test_missing_timestamp(self):
        history = [{"timestamp": "", "score": 50.0, "findings_count": 5}]
        result = _build_health_trend(Path("/tmp"), history)
        assert result[0]["date"] == "unknown"

    def test_score_rounded(self):
        history = [
            {"timestamp": "2026-01-01T00:00:00Z", "score": 75.333, "findings_count": 10}
        ]
        result = _build_health_trend(Path("/tmp"), history)
        assert result[0]["score"] == 75.3


class TestCollectRunMetrics:
    def test_no_runs_dir(self, tmp_path):
        result = _collect_run_metrics(tmp_path / "nonexistent")
        assert result["fix_attempts"] == 0
        assert result["fixes_verified"] == 0
        assert result["total_prs"] == 0
        assert result["runs"] == []

    def test_reads_run_files(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_data = {
            "id": "run-1",
            "phase": "scan",
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T01:00:00Z",
            "status": "completed",
            "findings_detected": 5,
            "issues_created": 2,
            "fix_attempts": 3,
            "fixes_verified": 1,
            "prs_created": 2,
            "health_before": 70.0,
            "health_after": 75.0,
            "health_delta": 5.0,
            "dry_run": False,
        }
        (runs_dir / "run-001.json").write_text(json.dumps(run_data))
        result = _collect_run_metrics(runs_dir)
        assert result["fix_attempts"] == 3
        assert result["fixes_verified"] == 1
        assert result["total_prs"] == 2
        assert len(result["runs"]) == 1
        assert result["runs"][0]["id"] == "run-1"
        assert result["runs"][0]["dry_run"] is False

    def test_skips_lock_files(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        (runs_dir / "run-001.lock").write_text("{}")
        result = _collect_run_metrics(runs_dir)
        assert result["runs"] == []

    def test_skips_corrupt_json(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        (runs_dir / "run-bad.json").write_text("NOT JSON{{{")
        result = _collect_run_metrics(runs_dir)
        assert result["runs"] == []

    def test_multiple_runs_aggregated(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        for i in range(3):
            (runs_dir / f"run-{i:03d}.json").write_text(
                json.dumps(
                    {
                        "fix_attempts": i + 1,
                        "fixes_verified": i,
                        "prs_created": 1,
                    }
                )
            )
        result = _collect_run_metrics(runs_dir)
        assert result["fix_attempts"] == 1 + 2 + 3
        assert result["total_prs"] == 3
        assert len(result["runs"]) == 3

    def test_missing_fields_default(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        (runs_dir / "run-min.json").write_text(json.dumps({}))
        result = _collect_run_metrics(runs_dir)
        assert result["runs"][0]["findings_detected"] == 0
        assert result["runs"][0]["phase"] == ""
        assert result["runs"][0]["dry_run"] is True

    def test_oserror_handled(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        bad_file = runs_dir / "run-err.json"
        bad_file.write_text("ok")
        bad_file.chmod(0o000)
        try:
            result = _collect_run_metrics(runs_dir)
            assert result["runs"] == []
        finally:
            bad_file.chmod(0o644)


class TestLoadReconciliationEvents:
    def test_extracts_events(self):
        state = {
            "reconciliation_events": [
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "reason": "stale_issue",
                    "before": {"open_issues": 10, "open_prs": 3},
                    "after": {"open_issues": 8, "open_prs": 3},
                }
            ]
        }
        result = _load_reconciliation_events(state)
        assert len(result) == 1
        assert result[0]["reason"] == "stale_issue"
        assert result[0]["before_open_issues"] == 10
        assert result[0]["after_open_issues"] == 8

    def test_empty_events(self):
        assert _load_reconciliation_events({}) == []
        assert _load_reconciliation_events({"reconciliation_events": []}) == []

    def test_null_before_after(self):
        state = {
            "reconciliation_events": [
                {"timestamp": "t", "reason": "r", "before": None, "after": None}
            ]
        }
        result = _load_reconciliation_events(state)
        assert result[0]["before_open_issues"] == 0

    def test_missing_before_after_keys(self):
        state = {"reconciliation_events": [{"timestamp": "t", "reason": "r"}]}
        result = _load_reconciliation_events(state)
        assert result[0]["before_open_issues"] == 0


class TestExtractReportData:
    def _setup_state(self, tmp_path, repo_name="test-repo"):
        state_dir = tmp_path / "repos" / repo_name / "state"
        state_dir.mkdir(parents=True)

        status = {
            "current_counts": {"open_issues": 5, "open_prs": 2},
            "health": {"score": 82.0},
            "language": "python",
            "last_run_at": "2026-01-15T10:00:00Z",
            "detector_catalog": [
                {"rule": "ruff-c408", "category": "lint", "language": "python"},
            ],
        }
        (state_dir / "status.json").write_text(json.dumps(status))

        findings = [
            {"rule": "ruff-c408", "path": "a.py", "line": 1, "severity": "medium"},
            {"rule": "ruff-c408", "path": "b.py", "line": 5, "severity": "medium"},
            {"rule": "unknown-rule", "path": "c.py", "line": 10, "severity": "low"},
        ]
        (state_dir / "findings.jsonl").write_text(
            "\n".join(json.dumps(f) for f in findings) + "\n"
        )

        issues = {"issues": [{"id": "issue-1", "status": "open"}]}
        (state_dir / "issues.json").write_text(json.dumps(issues))

        health_history = [
            {"timestamp": "2026-01-14T10:00:00Z", "score": 78.0, "findings_count": 5},
            {"timestamp": "2026-01-15T10:00:00Z", "score": 82.0, "findings_count": 3},
        ]
        (state_dir / "health_history.jsonl").write_text(
            "\n".join(json.dumps(h) for h in health_history) + "\n"
        )

        state = {
            "reconciliation_events": [
                {
                    "timestamp": "2026-01-15T12:00:00Z",
                    "reason": "auto",
                    "before": {"open_issues": 6, "open_prs": 2},
                    "after": {"open_issues": 5, "open_prs": 2},
                },
            ]
        }
        (state_dir / "state.json").write_text(json.dumps(state))

        config_dir = tmp_path / "repos" / repo_name
        (config_dir / "config.yaml").write_text(
            "url: https://github.com/example/test\n"
        )

        return state_dir

    def test_full_extraction(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        result = extract_report_data(
            repo_path="/tmp/test-repo",
            repo_name="test-repo",
            state_dir=state_dir,
        )
        assert result["repo"]["name"] == "test-repo"
        assert result["repo"]["health_score"] == 82.0
        assert result["repo"]["language"] == "python"
        assert result["summary"]["total_findings"] == 3
        assert result["summary"]["open_issues"] == 5
        assert result["summary"]["open_prs"] == 2
        assert result["repo"]["url"] == "https://github.com/example/test"
        assert len(result["health_trend"]) == 2
        assert len(result["reconciliation_events"]) == 1
        assert result["findings_by_category"]["lint"] == 2
        assert result["findings_by_category"]["other"] == 1
        assert len(result["top_rules"]) >= 1

    def test_uses_repos_dir_to_resolve_state(self, tmp_path):
        state_dir = self._setup_state(tmp_path)
        repos_dir = tmp_path / "repos"
        result = extract_report_data(
            repo_path="/tmp/test-repo",
            repo_name="test-repo",
            repos_dir=repos_dir,
        )
        assert result["summary"]["total_findings"] == 3

    def test_raises_when_no_dir_provided(self):
        with pytest.raises(ValueError, match="Either state_dir or repos_dir"):
            extract_report_data(repo_path="/tmp", repo_name="test")

    def test_empty_state_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["summary"]["total_findings"] == 0
        assert result["repo"]["health_score"] == 0

    def test_corrupt_status_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "status.json").write_text("BROKEN{")
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["summary"]["total_findings"] == 0

    def test_corrupt_findings_jsonl(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "findings.jsonl").write_text("NOT JSON\n{bad\n")
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["summary"]["total_findings"] == 0

    def test_health_score_from_health_history_fallback(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status = {"current_counts": {}, "language": "python"}
        (state_dir / "status.json").write_text(json.dumps(status))
        (state_dir / "health_history.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "score": 65.0,
                    "findings_count": 3,
                }
            )
            + "\n"
        )
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["repo"]["health_score"] == 65.0

    def test_health_score_from_current_counts_fallback(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status = {"current_counts": {"health_score": 55}, "language": "python"}
        (state_dir / "status.json").write_text(json.dumps(status))
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["repo"]["health_score"] == 55.0

    def test_canonical_categories_populated(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        expected = [
            "bug",
            "lint",
            "style",
            "security",
            "secret",
            "test",
            "docs",
            "performance",
            "type-safety",
            "refactor",
            "debt",
            "other",
        ]
        for cat in expected:
            assert cat in result["findings_by_category"]

    def test_generated_at_present(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["generated_at"]

    def test_last_scan_from_state_fallback(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state = {"last_run_at": "2026-02-01T00:00:00Z"}
        (state_dir / "state.json").write_text(json.dumps(state))
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["repo"]["last_scan"] == "2026-02-01T00:00:00Z"

    def test_days_parameter_limits_trend(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        history = [
            {
                "timestamp": f"2026-01-{i:02d}T00:00:00Z",
                "score": float(i),
                "findings_count": i,
            }
            for i in range(1, 20)
        ]
        (state_dir / "health_history.jsonl").write_text(
            "\n".join(json.dumps(h) for h in history) + "\n"
        )
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
            days=3,
        )
        assert len(result["health_trend"]) == 3


class TestToTemplateFormat:
    def test_maps_canonical_data(self):
        data = {
            "repo": {
                "name": "test-repo",
                "path": "/tmp/test",
                "language": "python",
                "health_score": 85.0,
            },
            "summary": {
                "total_findings": 10,
                "open_issues": 3,
                "open_prs": 1,
                "fixes_verified": 5,
            },
            "findings_by_category": {
                "bug": 2,
                "lint": 3,
                "style": 1,
                "security": 0,
                "secret": 0,
                "dead-code": 1,
                "simplify": 0,
                "refactor": 1,
                "other": 2,
                "test": 1,
                "docs": 1,
                "performance": 1,
                "type-safety": 1,
                "debt": 2,
            },
            "top_rules": [
                {
                    "rule": "ruff-c408",
                    "category": "lint",
                    "count": 3,
                    "severity": "low",
                },
            ],
            "health_trend": [
                {"date": "2026-01-01", "score": 80.0, "findings_count": 12},
            ],
            "findings_by_language": {"python": 8, "typescript": 2},
            "generated_at": "2026-01-15T00:00:00Z",
        }
        result = _to_template_format(data)
        assert result["repo"]["name"] == "test-repo"
        assert result["health_score"] == 85.0
        assert result["counts"]["total_findings"] == 10
        assert result["counts"]["findings_fixed"] == 5
        assert result["findings_by_category"]["bug"] == 2
        assert result["findings_by_category"]["dead-code"] == 1 + 2  # dead-code + debt
        assert (
            result["findings_by_category"]["refactor"] == 1 + 1 + 1
        )  # refactor + performance + type-safety
        assert (
            result["findings_by_category"]["other"] == 2 + 1 + 1
        )  # other + test + docs
        assert len(result["top_rules"]) == 1
        assert result["language_distribution"]["python"] == 8

    def test_safety_mode_watch_only_for_low_health(self):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": 25.0,
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fixes_verified": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "",
        }
        result = _to_template_format(data)
        assert result["repo"]["safety_mode"] == "watch-only"

    def test_safety_mode_active_for_high_health(self):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": 50.0,
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fixes_verified": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "",
        }
        result = _to_template_format(data)
        assert result["repo"]["safety_mode"] == "active"

    def test_top_rules_language_default(self):
        data = {
            "repo": {"name": "x", "path": "", "language": "python", "health_score": 80},
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fixes_verified": 0,
            },
            "findings_by_category": {},
            "top_rules": [
                {"rule": "r1", "category": "lint", "count": 1, "severity": "low"}
            ],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "",
        }
        result = _to_template_format(data)
        assert result["top_rules"][0]["language"] == "—"

    def test_top_rules_limited_to_20(self):
        rules = [
            {"rule": f"r-{i}", "category": "lint", "count": i, "severity": "low"}
            for i in range(30)
        ]
        data = {
            "repo": {"name": "x", "path": "", "language": "python", "health_score": 80},
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fixes_verified": 0,
            },
            "findings_by_category": {},
            "top_rules": rules,
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "",
        }
        result = _to_template_format(data)
        assert len(result["top_rules"]) == 20


class TestGeneratePlaceholderHtml:
    def test_generates_valid_html(self):
        data = {
            "repo": {
                "name": "test-repo",
                "path": "/tmp/test",
                "language": "python",
                "health_score": 85,
                "last_scan": "2026-01-15T10:00:00Z",
            },
            "summary": {
                "total_findings": 10,
                "open_issues": 3,
                "open_prs": 1,
                "fix_attempts": 5,
                "fixes_verified": 2,
                "total_prs": 1,
            },
            "findings_by_category": {"lint": 5, "bug": 3, "other": 2},
            "top_rules": [
                {"rule": "ruff-c408", "count": 5, "severity": "low"},
                {"rule": "secret-key", "count": 3, "severity": "high"},
            ],
            "health_trend": [
                {"date": "2026-01-14", "score": 80.0, "findings_count": 12},
                {"date": "2026-01-15", "score": 85.0, "findings_count": 10},
            ],
            "findings_by_language": {"python": 10},
            "generated_at": "2026-01-15T12:00:00Z",
        }
        html = _generate_placeholder_html(data)
        assert "<!DOCTYPE html>" in html
        assert "test-repo" in html
        assert "85" in html
        assert "Good" in html
        assert "ruff-c408" in html
        assert "secret-key" in html
        assert "2026-01-14" in html

    def test_critical_health_band(self):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": 10,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = _generate_placeholder_html(data)
        assert "Critical" in html

    def test_needs_work_health_band(self):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": 55,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = _generate_placeholder_html(data)
        assert "Needs Work" in html

    def test_poor_health_band(self):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": 35,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = _generate_placeholder_html(data)
        assert "Poor" in html

    def test_empty_findings_no_division_by_zero(self):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": 80,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = _generate_placeholder_html(data)
        assert "0" in html


class TestGenerateReportHtml:
    def test_fallback_when_no_template(self, tmp_path):
        data = {
            "repo": {
                "name": "test",
                "path": "",
                "language": "python",
                "health_score": 80,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 1,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {"lint": 1},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {"python": 1},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = generate_report_html(data, template_path=tmp_path / "nonexistent.html")
        assert "<!DOCTYPE html>" in html

    def test_with_valid_template(self, tmp_path):
        template = tmp_path / "template.html"
        template.write_text(
            '<html>const DATA = {\n  "key": "value"\n};\n\n// ═══ Render ═══\nrest</html>'
        )
        data = {
            "repo": {
                "name": "test",
                "path": "",
                "language": "python",
                "health_score": 80,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = generate_report_html(data, template_path=template)
        assert "const DATA = " in html
        assert "test" in html

    def test_template_without_markers_falls_back(self, tmp_path):
        template = tmp_path / "template.html"
        template.write_text("<html>no markers here</html>")
        data = {
            "repo": {
                "name": "test",
                "path": "",
                "language": "python",
                "health_score": 80,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = generate_report_html(data, template_path=template)
        assert "<!DOCTYPE html>" in html


# ── Gap-closing tests: corrupt data files, template edge cases ──


class TestExtractReportDataCorruptFiles:
    """Error handling for corrupt JSON/JSONL state files."""

    def test_corrupt_issues_json_handled(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "issues.json").write_text("BROKEN{")
        (state_dir / "status.json").write_text(json.dumps({"current_counts": {}}))
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["summary"]["open_issues"] == 0

    def test_corrupt_health_history_handled(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "status.json").write_text(json.dumps({"current_counts": {}}))
        (state_dir / "health_history.jsonl").write_text("NOT JSON{{{\n{bad\n")
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["health_trend"] == []
        assert result["repo"]["health_score"] == 0

    def test_corrupt_state_json_handled(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "status.json").write_text(json.dumps({"current_counts": {}}))
        (state_dir / "state.json").write_text("NOT JSON{{{")
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["reconciliation_events"] == []

    def test_empty_repo_path_string(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        result = extract_report_data(
            repo_path="",
            repo_name="test",
            state_dir=state_dir,
        )
        assert result["repo"]["path"] == ""
        assert result["repo"]["url"] is None

    def test_runs_dir_resolved_from_state_parent(self, tmp_path):
        state_dir = tmp_path / "repos" / "proj" / "state"
        state_dir.mkdir(parents=True)
        runs_dir = tmp_path / "repos" / "proj" / "runs"
        runs_dir.mkdir()
        (runs_dir / "run-001.json").write_text(
            json.dumps(
                {
                    "fix_attempts": 2,
                    "fixes_verified": 1,
                    "prs_created": 1,
                }
            )
        )
        (state_dir / "status.json").write_text(json.dumps({"current_counts": {}}))
        result = extract_report_data(
            repo_path="/tmp/test",
            repo_name="proj",
            state_dir=state_dir,
        )
        assert result["recent_runs"][0]["fix_attempts"] == 2


class TestCollectRunMetricsOrdering:
    """Runs are sorted reverse by filename (newest first)."""

    def test_sorted_reverse(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        for name in ["run-a.json", "run-b.json", "run-c.json"]:
            (runs_dir / name).write_text(json.dumps({"id": name}))
        result = _collect_run_metrics(runs_dir)
        assert result["runs"][0]["id"] == "run-c.json"
        assert result["runs"][-1]["id"] == "run-a.json"


class TestGenerateReportHtmlEdgeCases:
    """Template parsing edge cases."""

    def test_template_unclosed_brace_falls_back(self, tmp_path):
        """Template with only opening brace (no closing) triggers fallback."""
        template = tmp_path / "template.html"
        template.write_text(
            '<html>const DATA = {\n  "key": "value"\n\n// ═══ Render ═══\nrest</html>'
        )
        data = {
            "repo": {
                "name": "t",
                "path": "",
                "language": "py",
                "health_score": 80,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = generate_report_html(data, template_path=template)
        assert "<!DOCTYPE html>" in html

    def test_template_render_marker_after_data(self, tmp_path):
        """Template with proper markers and render section."""
        template = tmp_path / "template.html"
        template.write_text(
            '<html>\nconst DATA = {\n  "key": "value"\n};\n\n// ═══ Render ═══\n<body></body>\n</html>'
        )
        data = {
            "repo": {
                "name": "t",
                "path": "",
                "language": "py",
                "health_score": 80,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = generate_report_html(data, template_path=template)
        assert "const DATA = " in html
        assert "// ═══ Render ═══" in html


class TestPlaceholderHealthTrendColors:
    """Verify health trend color bands in placeholder HTML."""

    @pytest.mark.parametrize(
        "score,expected_band",
        [
            (85, "Good"),
            (60, "Needs Work"),
            (40, "Poor"),
            (15, "Critical"),
        ],
    )
    def test_health_score_bands(self, score, expected_band):
        data = {
            "repo": {
                "name": "x",
                "path": "",
                "language": "python",
                "health_score": score,
                "last_scan": "",
            },
            "summary": {
                "total_findings": 0,
                "open_issues": 0,
                "open_prs": 0,
                "fix_attempts": 0,
                "fixes_verified": 0,
                "total_prs": 0,
            },
            "findings_by_category": {},
            "top_rules": [],
            "health_trend": [],
            "findings_by_language": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        html = _generate_placeholder_html(data)
        assert expected_band in html


def test_pattern_replay_uses_canonical_language_inference():
    """Verify pattern_replay delegates to engine.report.infer_language_from_path."""
    from bluei.engine import pattern_replay
    from bluei.engine.report import infer_language_from_path

    # The local _infer_language_from_path should no longer exist
    assert not hasattr(pattern_replay, "_infer_language_from_path"), (
        "pattern_replay should use engine.report.infer_language_from_path, "
        "not a local copy"
    )

    # All extensions should resolve consistently
    for ext, expected in [
        (".py", "python"),
        (".ts", "typescript"),
        (".tsx", "typescript"),
        (".js", "javascript"),
        (".jsx", "javascript"),
        (".go", "go"),
        (".rs", "rust"),
    ]:
        assert infer_language_from_path(f"foo{ext}") == expected


# ── T3.3: Flywheel section in _generate_placeholder_html ──

FULL_LEDGER = {
    "findings_attempted": 25,
    "resolved_deterministic_by_stage": {
        "pattern-replay": 12,
        "recipe": 5,
        "autofix": 3,
    },
    "resolved_deterministic_total": 20,
    "resolved_llm": 3,
    "exhausted": 2,
    "pattern_replay_resolutions": 12,
    "savings_usd": 0.48,
    "cost_total_usd": 1.25,
    "active_pattern_count": 8,
    "top_failing_patterns": [
        {"pattern_id": "pat-001", "rule": "broad-except", "failure_count": 5},
        {"pattern_id": "pat-002", "rule": "ruff-c408", "failure_count": 3},
        {"pattern_id": "pat-003", "rule": "shellcheck-sc2086", "failure_count": 2},
    ],
}


def _base_data(**extra):
    """Minimal valid data dict for _generate_placeholder_html."""
    data = {
        "repo": {
            "name": "fw-repo",
            "path": "/tmp/fw",
            "language": "python",
            "health_score": 70,
            "last_scan": "2026-01-15T10:00:00Z",
        },
        "summary": {
            "total_findings": 10,
            "open_issues": 1,
            "open_prs": 0,
            "fix_attempts": 5,
            "fixes_verified": 2,
            "total_prs": 1,
        },
        "findings_by_category": {},
        "top_rules": [],
        "health_trend": [],
        "findings_by_language": {},
        "generated_at": "2026-01-15T12:00:00Z",
    }
    data.update(extra)
    return data


class TestFlywheelHtmlSection:
    """T3.3: Deterministic Flywheel section in _generate_placeholder_html."""

    def test_flywheel_section_present_when_ledger_populated(self):
        """Heading, per-stage, rate, savings, footnote all present."""
        html = _generate_placeholder_html(_base_data(flywheel_ledger=FULL_LEDGER))
        assert "Deterministic Flywheel" in html
        assert "20/25 (80.0%)" in html
        assert "3/25 (12.0%)" in html
        assert "$0.48" in html
        assert "pattern-replay" in html
        assert "recipe" in html
        assert "autofix" in html
        assert "12 resolving hits" in html
        assert "8 active patterns" in html
        assert "broad-except" in html
        assert "failure_count=5" in html

    def test_flywheel_section_absent_when_ledger_empty(self):
        """No flywheel section when ledger is empty dict."""
        html = _generate_placeholder_html(_base_data(flywheel_ledger={}))
        assert "Deterministic Flywheel" not in html

    def test_flywheel_section_absent_when_ledger_missing(self):
        """No flywheel section when key absent from data."""
        html = _generate_placeholder_html(_base_data())
        assert "Deterministic Flywheel" not in html

    def test_flywheel_divide_by_zero_shows_na(self):
        """attempted=0 renders n/a, not a crash."""
        zero_ledger = {
            "findings_attempted": 0,
            "resolved_deterministic_total": 0,
            "resolved_llm": 0,
            "exhausted": 0,
        }
        html = _generate_placeholder_html(_base_data(flywheel_ledger=zero_ledger))
        assert "Deterministic Flywheel" in html
        assert "n/a" in html

    def test_flywheel_adr0003_footnote_present(self):
        """ADR-0003 footnote verbatim when section renders."""
        html = _generate_placeholder_html(_base_data(flywheel_ledger=FULL_LEDGER))
        assert "Dollar savings reflect standalone Pattern-replay substitutions" in html
        assert "throughput, not cost" in html

    def test_flywheel_html_escapes_rule_names(self):
        """Rule names are HTML-escaped in the flywheel section."""
        ledger = {
            "findings_attempted": 1,
            "resolved_deterministic_total": 1,
            "resolved_llm": 0,
            "exhausted": 0,
            "top_failing_patterns": [
                {"rule": "<script>alert(1)</script>", "failure_count": 1},
            ],
        }
        html = _generate_placeholder_html(_base_data(flywheel_ledger=ledger))
        assert "&lt;script&gt;" in html
        # The escaped version appears in the flywheel section;
        # the raw JSON <details> dump may contain the unescaped form.

    def test_flywheel_cost_section_present(self):
        """Cost block renders when cost/savings are nonzero."""
        html = _generate_placeholder_html(_base_data(flywheel_ledger=FULL_LEDGER))
        assert "$1.25 spent" in html
        assert "$0.48 avoided" in html
