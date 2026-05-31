"""Tests for bluei/engine/orchestrator.py — discovery, routing, suppression, escalation.

Focuses on behavior: what findings are produced, how they're routed, how cycle
signals suppress, and how escalation detects failure streaks. Command builders
are tested via parametrize — each builder must produce the correct phase name.
"""

import argparse
import json
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.models import now_iso, stable_finding_id


# ── Helpers ──────────────────────────────────────────────────────────────────


def _mf(make_finding, **overrides):
    """Wrap conftest make_finding with orchestrator-specific defaults + stable_finding_id."""
    defaults = {
        "rule": "discount-math-sign",
        "path": "price.py",
        "line": 5,
        "confidence": 0.95,
        "safe_to_autofix": True,
        "quick_win": True,
        "snippet": "return amount + discount",
        "repo": "test-repo",
    }
    defaults.update(overrides)
    if "finding_id" not in overrides:
        defaults["finding_id"] = stable_finding_id(
            defaults["repo"],
            defaults["path"],
            defaults["line"],
            defaults["rule"],
            defaults["snippet"],
        )
    return make_finding(**defaults)


def _build_mock_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        repo_path=Path("/tmp/repo"),
        state_file=Path("/tmp/state.json"),
        log_file=Path("/tmp/run.log"),
        findings_file=Path("/tmp/findings.jsonl"),
        issues_file=Path("/tmp/issues.json"),
        worktree_root=Path("/tmp/worktrees"),
        open_issues_cap=20,
        open_prs_cap=10,
        issue_confidence_threshold=0.8,
        max_files_changed=8,
        max_loc_diff=500,
        max_prs_per_run=5,
        max_issues_per_run=10,
        finding_cooldown_seconds=14400,
        merge_cooldown_minutes=30,
        max_fix_attempts_per_issue=3,
        docs_index_file=Path("/tmp/docs_index.json"),
        fix_engine="deterministic",
        claude_cmd_template="claude --print",
        run_phase="active-cycle",
        refresh_docs_index=False,
        live_github_actions=False,
        auto_merge_sandbox=False,
        dry_run=True,
        max_split_depth=3,
        max_queue_items=0,
        auto_approve=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _patch_all_discovery(**scanner_overrides):
    """Return a list of patches that silence every sub-scanner in discover_findings.

    Pass keyword overrides to inject specific findings from a scanner, e.g.
    ``_patch_all_discovery(_ast_scan_go_files=[f_go])``.

    Pass ``None`` as a value to skip patching that scanner entirely (useful
    when the scanner is the system under test, e.g. load_docs_index in docs
    index tests).
    """
    defaults = {
        "_ast_scan_python_files": [],
        "_ast_scan_ts_js_files": [],
        "_ast_scan_go_files": [],
        "_ast_scan_rust_files": [],
        "discover_typescript_type_findings": [],
        "discover_test_coverage_findings": [],
        "discover_xo_linter_findings": [],
        "discover_python_linter_findings": [],
        "run_plugin_discovery": [],
        "load_docs_index": [],
    }
    defaults.update(scanner_overrides)
    return [
        patch(f"bluei.engine.orchestrator.{k}", return_value=v)
        for k, v in defaults.items()
        if v is not None
    ]


def _run_discovery(repo_path, log_path, **scanner_overrides):
    """Run discover_findings with all sub-scanners patched out (except overrides)."""
    from bluei.engine.orchestrator import discover_findings

    with ExitStack() as stack:
        for p in _patch_all_discovery(**scanner_overrides):
            stack.enter_context(p)
        return discover_findings(repo_path, log_file=log_path)


# ── Command Builders (parametrized) ─────────────────────────────────────────


_CMD_BUILDERS = [
    ("build_active_cycle_command", "active-cycle"),
    ("build_issue_cycle_command", "issue-cycle"),
    ("build_pr_cycle_command", "pr-cycle"),
    ("build_merge_cycle_command", "merge-cycle"),
    ("build_orchestrated_cycle_command", "orchestrated"),
    ("build_refactor_cycle_command", "refactor-cycle"),
    ("build_verification_only_command", "verify-only"),
    ("build_docs_index_refresh_command", "docs-index"),
]


@pytest.mark.parametrize(
    "builder_name, expected_phase",
    _CMD_BUILDERS,
    ids=[f"{name}" for name, _ in _CMD_BUILDERS],
)
def test_command_builder_produces_correct_phase(builder_name, expected_phase):
    """Each command builder must produce a shell command containing its phase name."""
    import bluei.engine.orchestrator as orch

    builder = getattr(orch, builder_name)
    args = _build_mock_args()
    cmd = builder(args)
    assert expected_phase in cmd


def test_reconcile_only_command_structure():
    """Reconcile-only uses --reconcile-only, not --run-phase."""
    from bluei.engine.orchestrator import build_reconcile_only_command

    cmd = build_reconcile_only_command(_build_mock_args())
    assert "--reconcile-only" in cmd
    assert "--run-phase" not in cmd


@pytest.mark.parametrize(
    "flag_name, builder_name, flag_str",
    [
        ("refresh_docs_index", "build_active_cycle_command", "--refresh-docs-index"),
        ("live_github_actions", "build_active_cycle_command", "--live-github-actions"),
        ("auto_merge_sandbox", "build_issue_cycle_command", "--auto-merge-sandbox"),
        (
            "live_github_actions",
            "build_verification_only_command",
            "--live-github-actions",
        ),
    ],
)
def test_conditional_flags_appear_when_enabled(flag_name, builder_name, flag_str):
    """Flags like --refresh-docs-index only appear when the corresponding arg is True."""
    import bluei.engine.orchestrator as orch

    builder = getattr(orch, builder_name)
    cmd_without = builder(_build_mock_args(**{flag_name: False}))
    cmd_with = builder(_build_mock_args(**{flag_name: True}))
    assert flag_str not in cmd_without
    assert flag_str in cmd_with


def test_refactor_cycle_includes_optional_args():
    from bluei.engine.orchestrator import build_refactor_cycle_command

    cmd = build_refactor_cycle_command(
        _build_mock_args(max_queue_items=5, auto_approve=True)
    )
    assert "--max-queue-items" in cmd
    assert "--auto-approve" in cmd


# ── Discovery: string detectors produce correct findings ────────────────────


class TestDiscoverFindingsDetectors:
    """Verify that discover_findings detects the right rules from real file content."""

    @pytest.mark.parametrize(
        "file_path, content, expected_rule",
        [
            (
                "price.py",
                "def calc(amount, discount):\n    return amount + discount\n",
                "discount-math-sign",
            ),
            ("price.py", "x = 1   \n", "trailing-whitespace"),
        ],
        ids=["discount-math-sign", "trailing-whitespace"],
    )
    def test_python_detectors(self, tmp_path, file_path, content, expected_rule):
        (tmp_path / file_path).write_text(content)
        findings = _run_discovery(tmp_path, tmp_path / "log.txt")
        assert expected_rule in [f.rule for f in findings]

    def test_broad_except(self, tmp_path):
        orders_dir = tmp_path / "src" / "qa_sandbox"
        orders_dir.mkdir(parents=True)
        (orders_dir / "orders.py").write_text(
            "try:\n    pass\nexcept Exception:\n    pass\n"
        )
        findings = _run_discovery(tmp_path, tmp_path / "log.txt")
        assert "broad-except" in [f.rule for f in findings]

    def test_docs_legacy_reference(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "ARCHITECTURE.md").write_text(
            "# Arch\nUse legacy_pricer.py for pricing.\n"
        )
        findings = _run_discovery(tmp_path, tmp_path / "log.txt")
        assert "docs-legacy-reference" in [f.rule for f in findings]

    def test_todo_marker(self, tmp_path):
        cat_dir = tmp_path / "src" / "qa_sandbox"
        cat_dir.mkdir(parents=True)
        (cat_dir / "catalog.py").write_text("# TODO: fix this\n")
        findings = _run_discovery(tmp_path, tmp_path / "log.txt")
        assert "debt-todo-marker" in [f.rule for f in findings]


# ── Discovery: AST scan directory filtering ──────────────────────────────────


class TestAstScanSkipsDirs:
    def test_skips_venv_dirs(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files

        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "evil.py").write_text("import os\nos.system('rm -rf /')\n")
        with patch("bluei.engine.ast_engine.get_python_matcher") as mock:
            mock.return_value.find_matches.return_value = []
            _ast_scan_python_files(tmp_path, tmp_path / "log.txt", {})
        mock.return_value.find_matches.assert_not_called()

    def test_skips_empty_files(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files

        (tmp_path / "empty.py").write_text("   \n")
        with patch("bluei.engine.ast_engine.get_python_matcher") as mock:
            mock.return_value.find_matches.return_value = []
            _ast_scan_python_files(tmp_path, tmp_path / "log.txt", {})
        mock.return_value.find_matches.assert_not_called()

    def test_ast_scan_produces_findings_from_matches(self, tmp_path):
        from bluei.engine.orchestrator import _ast_scan_python_files

        (tmp_path / "good.py").write_text("x = 1\n")
        match_mock = MagicMock()
        match_mock.pattern.id = "hardcoded-tmp-path"
        match_mock.line = 1
        match_mock.source_text = 'Path("/tmp/foo")'

        with patch("bluei.engine.ast_engine.get_python_matcher") as mock:
            mock.return_value.find_matches.return_value = [match_mock]
            findings = _ast_scan_python_files(
                tmp_path,
                tmp_path / "log.txt",
                {"hardcoded-tmp-path": {"confidence": 0.9, "autofix": True}},
            )
        assert len(findings) == 1
        assert findings[0].rule == "hardcoded-tmp-path"

    def test_alternate_rule_id_mapping(self, tmp_path):
        """hardcoded-tmp-path-string maps to the hardcoded-tmp-path catalog entry."""
        from bluei.engine.orchestrator import _ast_scan_python_files

        (tmp_path / "code.py").write_text("x = 1\n")
        match_mock = MagicMock()
        match_mock.pattern.id = "hardcoded-tmp-path-string"
        match_mock.line = 1
        match_mock.source_text = 'Path("/tmp/x")'

        with patch("bluei.engine.ast_engine.get_python_matcher") as mock:
            mock.return_value.find_matches.return_value = [match_mock]
            findings = _ast_scan_python_files(
                tmp_path,
                tmp_path / "log.txt",
                {"hardcoded-tmp-path": {"confidence": 0.9, "autofix": True}},
            )
        assert len(findings) == 1


# ── Discovery: multi-language scanner aggregation ────────────────────────────


class TestDiscoverFindingsMultiLanguage:
    """discover_findings aggregates findings from all language scanners."""

    @pytest.mark.parametrize(
        "scanner_key, file_path, rule",
        [
            ("_ast_scan_ts_js_files", "app.ts", "ts-rule"),
            ("_ast_scan_go_files", "main.go", "go-rule"),
            ("_ast_scan_rust_files", "main.rs", "rust-rule"),
            ("run_plugin_discovery", "mod.rs", "custom-plugin-rule"),
        ],
        ids=["ts-ast", "go-ast", "rust-ast", "plugin"],
    )
    def test_scanner_findings_aggregated(self, tmp_path, scanner_key, file_path, rule, make_finding):
        f = _mf(make_finding, path=file_path, rule=rule)
        findings = _run_discovery(tmp_path, tmp_path / "log.txt", **{scanner_key: [f]})
        assert any(finding.rule == rule for finding in findings)


def test_discover_findings_skips_internal_env(tmp_path):
    """When --skip-internal-discovery is set, discovery returns empty."""
    from bluei.engine.orchestrator import discover_findings

    with patch.dict(os.environ, {"--skip-internal-discovery": "1"}):
        result = discover_findings(tmp_path, log_file=tmp_path / "log.txt")
    assert result == []


# ── Discovery: docs index integration ────────────────────────────────────────


class TestDiscoverFindingsDocsIndex:
    """Tests for docs-index-backed gap/drift detectors.

    NOTE: We patch all sub-scanners EXCEPT load_docs_index, because the docs
    index file is the input under test — discover_findings must be allowed to
    read it for these tests to work.
    """

    def _mock_base_patches(self):
        """Patches for all sub-scanners except load_docs_index."""
        return _patch_all_discovery(load_docs_index=None)

    def _apply_patches(self, stack):
        """Apply base patches, filtering out None entries (intentionally unpatched)."""
        for p in self._mock_base_patches():
            if p is not None:
                stack.enter_context(p)

    def test_docs_index_uncovered_module(self, tmp_path):
        (tmp_path / "price.py").write_text("x = 1\n")
        code_dir = tmp_path / "src"
        code_dir.mkdir()
        (code_dir / "module.py").write_text("def foo(): pass\n")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text(
            json.dumps([{"code_path": "src/module.py", "coverage_status": "uncovered"}])
        )

        from bluei.engine.orchestrator import discover_findings

        with ExitStack() as stack:
            self._apply_patches(stack)
            findings = discover_findings(
                tmp_path, log_file=tmp_path / "log.txt", docs_index_file=docs_index
            )
        assert "doc-gap-uncovered-module" in [f.rule for f in findings]

    def test_docs_index_drift_detected(self, tmp_path):
        (tmp_path / "price.py").write_text("x = 1\n")
        (tmp_path / "app.py").write_text("print('hello')\n")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text(
            json.dumps(
                [
                    {
                        "code_path": "app.py",
                        "coverage_status": "covered",
                        "has_external_doc_ref": True,
                        "last_seen_sha": "oldsha123",
                        "last_updated": "2020-01-01T00:00:00+00:00",
                    }
                ]
            )
        )

        from bluei.engine.orchestrator import discover_findings

        with ExitStack() as stack:
            self._apply_patches(stack)
            stack.enter_context(
                patch(
                    "bluei.engine.orchestrator._git_last_commit_for_path",
                    return_value="newsha456",
                )
            )
            findings = discover_findings(
                tmp_path, log_file=tmp_path / "log.txt", docs_index_file=docs_index
            )
        assert "doc-drift-stale-reference" in [f.rule for f in findings]

    @pytest.mark.parametrize(
        "index_content, should_find_gap",
        [
            (
                json.dumps(
                    [{"code_path": "nonexistent.py", "coverage_status": "uncovered"}]
                ),
                False,
            ),
            (json.dumps([{"code_path": ""}]), False),
        ],
        ids=["missing-file", "empty-path"],
    )
    def test_invalid_entries_skipped(self, tmp_path, index_content, should_find_gap):
        (tmp_path / "price.py").write_text("x = 1\n")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text(index_content)

        from bluei.engine.orchestrator import discover_findings

        with ExitStack() as stack:
            self._apply_patches(stack)
            findings = discover_findings(
                tmp_path, log_file=tmp_path / "log.txt", docs_index_file=docs_index
            )
        assert any(f.rule.startswith("doc-") for f in findings) == should_find_gap


# ── Routing: route_findings_with_intent ──────────────────────────────────────


class TestRouteFindingsWithIntent:
    def test_below_confidence_is_skipped(self, make_finding):
        from bluei.engine.orchestrator import route_findings_with_intent

        f = _mf(make_finding, confidence=0.3)
        result = route_findings_with_intent([f], 0.8)
        assert len(result["skipped"]) == 1
        assert result["autofix_safe"] == []
        assert result["human_review"] == []

    def test_safe_autofix_routed_correctly(self, make_finding):
        from bluei.engine.orchestrator import route_findings_with_intent

        f = _mf(make_finding, 
            confidence=0.95,
            safe_to_autofix=True,
            quick_win=True,
            rule="trailing-whitespace",
        )
        result = route_findings_with_intent([f], 0.8)
        # Should end up in autofix_safe or human_review (depending on classify_finding)
        assert len(result["autofix_safe"]) + len(result["human_review"]) == 1

    def test_non_autofix_goes_to_human_review(self, make_finding):
        from bluei.engine.orchestrator import route_findings_with_intent

        f = _mf(make_finding, 
            confidence=0.95,
            safe_to_autofix=False,
            quick_win=False,
            rule="complex-thing",
        )
        result = route_findings_with_intent([f], 0.8)
        assert len(result["human_review"]) == 1

    def test_refactor_queue_with_worktree_not_allowed(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import route_findings_with_intent
        from bluei.engine.reforge import RefactorClass

        f = _mf(make_finding, rule="max-lines", confidence=0.95, safe_to_autofix=False)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        mock_entry = MagicMock()
        mock_entry.work_id = "w1"

        with (
            patch(
                "bluei.engine.orchestrator.classify_finding",
                return_value=RefactorClass.REFACTOR_CLASS,
            ),
            patch(
                "bluei.engine.orchestrator.can_auto_refactor",
                return_value=(False, "too-large"),
            ),
            patch(
                "bluei.engine.orchestrator.enqueue_refactor_work",
                return_value=mock_entry,
            ),
            patch("bluei.engine.orchestrator.save_refactor_work"),
        ):
            result = route_findings_with_intent(
                [f],
                0.8,
                findings_file=tmp_path / "f.jsonl",
                worktree_path=worktree,
                log_file=tmp_path / "log.txt",
            )
        assert len(result["refactor_queue"]) == 1
        assert result["refactor_queue"][0]["reason"] == "too-large"
        assert result["refactor_queue"][0]["queued_work_id"] == "w1"

    def test_refactor_queue_auto_refactor_allowed(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import route_findings_with_intent
        from bluei.engine.reforge import RefactorClass

        f = _mf(make_finding, rule="xo-complexity", confidence=0.95, safe_to_autofix=False)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with (
            patch(
                "bluei.engine.orchestrator.classify_finding",
                return_value=RefactorClass.REFACTOR_CLASS,
            ),
            patch(
                "bluei.engine.orchestrator.can_auto_refactor", return_value=(True, "ok")
            ),
            patch("bluei.engine.orchestrator.save_refactor_work"),
        ):
            result = route_findings_with_intent(
                [f],
                0.8,
                findings_file=tmp_path / "f.jsonl",
                worktree_path=worktree,
                log_file=tmp_path / "log.txt",
            )
        assert len(result["refactor_queue"]) == 1
        assert result["refactor_queue"][0]["reason"] == "planning"


# ── Routing: choose_safe_autofix_items ──────────────────────────────────────


def test_choose_safe_autofix_filters_by_safety_and_confidence(make_finding):
    from bluei.engine.orchestrator import choose_safe_autofix_items

    f1 = _mf(make_finding, safe_to_autofix=True, confidence=0.95)
    f2 = _mf(make_finding, path="other.py", safe_to_autofix=False, confidence=0.95)
    f3 = _mf(make_finding, path="third.py", safe_to_autofix=True, confidence=0.3)
    result = choose_safe_autofix_items([f1, f2, f3], 0.8)
    assert len(result) == 1
    assert result[0].finding_id == f1.finding_id


# ── Issue creation with cycle signal suppression ─────────────────────────────


class TestCreateIssuesWithCycleSignals:
    def _make_signal_file(self, tmp_path, suppressed_rules):
        signal_file = tmp_path / "cycle_signals.json"
        signal_file.write_text(json.dumps({"suppressed_rules": suppressed_rules}))
        return signal_file

    def test_global_suppression_skips_all(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        signal_file = self._make_signal_file(
            tmp_path,
            {
                "__global__": {
                    "reason": "cooldown",
                    "expires_at": "2099-12-31T00:00:00Z",
                }
            },
        )
        finding = _mf(make_finding, confidence=0.95)
        created = create_issues_for_findings(
            {"issues": []}, [finding], 0.8, 10, cycle_signals_path=signal_file
        )
        assert len(created) == 1
        assert created[0]["issue_id"] == "SUPPRESSED"
        assert created[0]["status"] == "suppressed_cross_cycle"

    def test_rule_suppression_skips_matching_only(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        signal_file = self._make_signal_file(
            tmp_path,
            {
                "discount-math-sign": {
                    "reason": "too many",
                    "expires_at": "2099-12-31T00:00:00Z",
                }
            },
        )
        f_suppressed = _mf(make_finding, confidence=0.95, rule="discount-math-sign")
        f_other = _mf(make_finding, confidence=0.95, rule="other-rule", line=99)
        created = create_issues_for_findings(
            {"issues": []},
            [f_suppressed, f_other],
            0.8,
            10,
            cycle_signals_path=signal_file,
        )
        assert len(created) == 2
        suppressed = [c for c in created if c["issue_id"] == "SUPPRESSED"]
        assert len(suppressed) == 1
        assert suppressed[0]["rule"] == "discount-math-sign"

    def test_expired_suppression_not_applied(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        signal_file = self._make_signal_file(
            tmp_path,
            {"__global__": {"reason": "old", "expires_at": "2020-01-01T00:00:00Z"}},
        )
        created = create_issues_for_findings(
            {"issues": []},
            [_mf(make_finding, confidence=0.95)],
            0.8,
            10,
            cycle_signals_path=signal_file,
        )
        assert len(created) == 1
        assert created[0]["status"] == "open"

    @pytest.mark.parametrize(
        "file_content, label",
        [("not json", "corrupt"), ("", "missing")],
        ids=["corrupt", "missing"],
    )
    def test_bad_signal_file_does_not_error(self, tmp_path, file_content, label, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        signal_file = tmp_path / "signals.json"
        if file_content:
            signal_file.write_text(file_content)
        # If file_content is empty string, file doesn't exist — that's the "missing" case
        created = create_issues_for_findings(
            {"issues": []},
            [_mf(make_finding, confidence=0.95)],
            0.8,
            10,
            cycle_signals_path=signal_file,
        )
        assert len(created) == 1


# ── Ensure issue for finding ─────────────────────────────────────────────────


class TestEnsureIssueForFinding:
    def test_returns_existing_issue(self, make_finding):
        from bluei.engine.orchestrator import ensure_issue_for_finding

        f = _mf(make_finding)
        issue = {"finding_id": f.finding_id, "status": "open"}
        issues_data = {"issues": [issue]}
        assert ensure_issue_for_finding(issues_data, f, 0.5) is issue

    def test_creates_new_issue_when_absent(self, make_finding):
        from bluei.engine.orchestrator import ensure_issue_for_finding

        f = _mf(make_finding, confidence=0.95)
        issues_data = {"issues": []}
        result = ensure_issue_for_finding(issues_data, f, 0.5)
        assert result is not None
        assert result["status"] == "open"
        assert result["finding_id"] == f.finding_id
        assert len(issues_data["issues"]) == 1

    def test_below_threshold_returns_none(self, make_finding):
        from bluei.engine.orchestrator import ensure_issue_for_finding

        f = _mf(make_finding, confidence=0.3)
        result = ensure_issue_for_finding({"issues": []}, f, 0.8)
        assert result is None


# ── Consecutive fix failure detection ────────────────────────────────────────


class TestCheckConsecutiveFixFailures:
    def test_returns_true_on_threshold_met(self):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "open"},
                {"event": "fix_failed_verification"},
                {"event": "fix_failed_verification"},
                {"event": "fix_failed_verification"},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is True

    @pytest.mark.parametrize(
        "reset_event",
        ["resolved_verified", "open"],
        ids=["success-resets", "reopen-resets"],
    )
    def test_success_events_reset_counter(self, reset_event):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "fix_failed_verification"},
                {"event": "fix_failed_verification"},
                {"event": reset_event},
                {"event": "fix_failed_verification"},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is False

    def test_needs_human_events_count_as_failures(self):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "open"},
                {"event": "needs-human-scope-exceeded"},
                {"event": "needs-human-push-failed"},
                {"event": "needs-human-commit-failed"},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is True


# ── Escalation logic ────────────────────────────────────────────────────────


class TestCheckFindingEscalation:
    def _make_failing_issue(self, n_failures=3):
        history = [{"event": "open"}]
        for _ in range(n_failures):
            history.append({"event": "fix_failed_verification"})
        return {
            "history": history,
            "finding_id": "x",
            "issue_id": "QA-1",
            "rule": "r",
        }

    def test_no_escalation_when_no_failures(self):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        issue = {
            "history": [{"event": "open"}],
            "finding_id": "x",
            "issue_id": "QA-1",
            "rule": "r",
        }
        assert check_finding_escalation_before_fix(issue, {}) is False

    def test_escalation_logs_to_file(self, tmp_path):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        log = tmp_path / "log.txt"
        result = check_finding_escalation_before_fix(
            self._make_failing_issue(), {}, 3, log_file=log
        )
        assert result is True
        assert log.exists()

    def test_escalation_logs_to_config(self):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        mock_config = MagicMock()
        with patch("bluei.engine.escalation.log_escalation_event"):
            result = check_finding_escalation_before_fix(
                self._make_failing_issue(), {}, 3, escalation_config=mock_config
            )
        assert result is True

    def test_escalation_config_exception_handled_gracefully(self):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        mock_config = MagicMock()
        with patch(
            "bluei.engine.escalation.log_escalation_event",
            side_effect=Exception("boom"),
        ):
            result = check_finding_escalation_before_fix(
                self._make_failing_issue(), {}, 3, escalation_config=mock_config
            )
        assert result is True


# ── Failed fix attempt counting ──────────────────────────────────────────────


class TestCountFailedFixAttempts:
    @pytest.mark.parametrize(
        "history, expected_count",
        [
            ([], 0),
            (None, 0),
            (
                [
                    {"event": "open"},
                    {"event": "fix_failed_verification"},
                    {"event": "needs-human-push-failed"},
                    {"event": "something_else"},
                    {"event": "needs-human-custom-reason"},
                ],
                3,
            ),
        ],
        ids=["empty", "no-key", "mixed"],
    )
    def test_counting(self, history, expected_count):
        from bluei.engine.orchestrator import count_failed_fix_attempts

        issue = {} if history is None else {"history": history}
        assert count_failed_fix_attempts(issue) == expected_count


# ── Issue history helpers ────────────────────────────────────────────────────


class TestIssueHistoryHelpers:
    def test_append_and_set_status(self):
        from bluei.engine.orchestrator import append_issue_history, set_issue_status

        issue = {}
        append_issue_history(issue, "open")
        assert len(issue["history"]) == 1
        assert issue["history"][0]["event"] == "open"

        set_issue_status(issue, "resolved", detail="merge conflict")
        assert issue["status"] == "resolved"
        assert issue["status_detail"] == "merge conflict"
        assert len(issue["history"]) == 2  # open + resolved


# ── AST scan: TS/JS, Go, Rust ───────────────────────────────────────────────


class TestAstScanTsJsGoRust:
    """Parametrized tests for TS/JS, Go, and Rust AST scanners.

    Each scanner:
    - Returns [] when tree-sitter is unavailable
    - Produces findings when matches are found
    - Skips known build/vendor dirs
    """

    # (scan_func_name, get_matcher_name, test_file_name, content, rule_id)
    _SCAN_CONFIGS = [
        (
            "_ast_scan_ts_js_files",
            "get_ts_matcher",
            "app.ts",
            "const x: any = 1;\n",
            "type-explicit-any",
        ),
        (
            "_ast_scan_go_files",
            "get_go_matcher",
            "main.go",
            "package main\nfunc main() {}\n",
            "go-rule",
        ),
        (
            "_ast_scan_rust_files",
            "get_rust_matcher",
            "main.rs",
            "fn main() {}\n",
            "rust-rule",
        ),
    ]

    @pytest.mark.parametrize(
        "scan_func_name, get_matcher_name, file_name, content, rule_id",
        _SCAN_CONFIGS,
        ids=["ts-js", "go", "rust"],
    )
    def test_returns_empty_when_unavailable(
        self, tmp_path, scan_func_name, get_matcher_name, file_name, content, rule_id
    ):
        import bluei.engine.orchestrator as orch

        func = getattr(orch, scan_func_name)
        with patch(
            "bluei.engine.ast_engine.ts_parser.TreeSitterAdapter.is_available",
            return_value=False,
        ):
            assert func(tmp_path, tmp_path / "log.txt", {}) == []

    @pytest.mark.parametrize(
        "scan_func_name, get_matcher_name, file_name, content, rule_id",
        _SCAN_CONFIGS,
        ids=["ts-js", "go", "rust"],
    )
    def test_produces_findings_from_matches(
        self, tmp_path, scan_func_name, get_matcher_name, file_name, content, rule_id
    ):
        import bluei.engine.orchestrator as orch

        (tmp_path / file_name).write_text(content)
        match_mock = MagicMock()
        match_mock.pattern.id = rule_id
        match_mock.line = 1
        match_mock.source_text = content.strip()

        with (
            patch(
                "bluei.engine.ast_engine.ts_parser.TreeSitterAdapter.is_available",
                return_value=True,
            ),
            patch(f"bluei.engine.ast_engine.{get_matcher_name}") as mock_get,
        ):
            mock_get.return_value.find_matches.return_value = [match_mock]
            findings = getattr(orch, scan_func_name)(
                tmp_path,
                tmp_path / "log.txt",
                {rule_id: {"confidence": 0.9, "autofix": False}},
            )
        assert len(findings) == 1

    # (scan_func_name, get_matcher_name, skip_file_name, content)
    _SKIP_CONFIGS = [
        ("_ast_scan_ts_js_files", "get_ts_matcher", "dist/bundle.js", "var x = 1;\n"),
        ("_ast_scan_go_files", "get_go_matcher", "vendor/lib.go", "package vendor\n"),
    ]

    @pytest.mark.parametrize(
        "scan_func_name, get_matcher_name, skip_file_name, content",
        _SKIP_CONFIGS,
        ids=["ts-js-skip-dist", "go-skip-vendor"],
    )
    def test_skips_build_and_vendor_dirs(
        self, tmp_path, scan_func_name, get_matcher_name, skip_file_name, content
    ):
        import bluei.engine.orchestrator as orch

        skip_path = tmp_path / skip_file_name
        skip_path.parent.mkdir(parents=True, exist_ok=True)
        skip_path.write_text(content)

        with (
            patch(
                "bluei.engine.ast_engine.ts_parser.TreeSitterAdapter.is_available",
                return_value=True,
            ),
            patch(f"bluei.engine.ast_engine.{get_matcher_name}") as mock,
        ):
            getattr(orch, scan_func_name)(tmp_path, tmp_path / "log.txt", {})
        mock.return_value.find_matches.assert_not_called()
