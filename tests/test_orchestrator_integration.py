"""Integration tests for bluei.engine.orchestrator.

Covers:
1. discover_findings() end-to-end with mocked external tools
2. Cross-source deduplication (string vs AST)
3. Command builders (build_pr_cycle_command, build_merge_cycle_command,
   build_docs_index_refresh_command)
4. Issue lifecycle helpers (create_issues_for_findings,
   choose_safe_autofix_items, check_consecutive_fix_failures,
   check_finding_escalation_before_fix)
"""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bluei.engine.models import Finding, now_iso, stable_finding_id


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


def _build_standard_repo(tmp_path: Path) -> Path:
    """Create a temp repo with all files that string detectors scan."""
    repo = tmp_path / "repo"
    repo.mkdir()

    (repo / "price.py").write_text(
        "def calculate_discount(amount, discount):\n    return amount + discount\n"
    )

    qa = repo / "src" / "qa_sandbox"
    qa.mkdir(parents=True)

    (qa / "catalog.py").write_text(
        "def find_item(items, query):\n    for item in items:\n        if item == query:\n            return item\n"
    )

    (qa / "orders.py").write_text(
        "def compute_tax(order):\n    tax = int(order.subtotal * order.tax_rate)\n"
        "    try:\n        pass\n    except Exception:\n        pass\n"
    )

    (qa / "notifications.py").write_text(
        "def normalize_email(value):\n    return value.lower()\n"
    )

    (qa / "inventory.py").write_text(
        "def reserve_stock(stock, sku, quantity):\n"
        "    if stock[sku] < quantity:\n        return False\n"
        "    queue = []\n    while queue:\n        task = queue.pop(0)\n"
    )

    (qa / "analytics.py").write_text(
        "def check_seen(users, seen):\n    for user in users:\n        if user not in seen:\n            seen.append(user)\n"
    )

    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "report_health.py").write_text(
        "from pathlib import Path\nconfig = Path('/tmp/qa-sandbox-state.json')\n"
    )

    docs = repo / "docs"
    docs.mkdir()
    (docs / "ARCHITECTURE.md").write_text(
        "# Architecture\nSee legacy_pricer.py for details.\n"
    )
    (docs / "TROUBLESHOOTING.md").write_text(
        "# Troubleshooting\nlegacy_pricer.py handles pricing.\n"
    )
    (docs / "OPERATIONS.md").write_text("# Operations\n## Rollback\ngit revert HEAD\n")

    (repo / "README.md").write_text("# Project\npytest -q\npip install pytest\n")

    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_orders.py").write_text("def test_order():\n    pass\n")
    (tests / "test_inventory.py").write_text("def test_reserve():\n    pass\n")

    return repo


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
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ────────────────────────────────────────────────────────────────
# 1. discover_findings() end-to-end
# ────────────────────────────────────────────────────────────────


class TestDiscoverFindingsEndToEnd:
    def test_string_detectors_fire(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        rules = {f.rule for f in findings}
        assert "orders-tax-truncation" in rules
        assert "docs-legacy-reference" in rules

    def test_todo_markers_detected(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        (repo / "src" / "qa_sandbox" / "catalog.py").write_text(
            "# TODO: refactor this\n# FIXME: broken logic\ndef find_item(items, query):\n    pass\n"
        )
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        todo_findings = [f for f in findings if f.rule == "debt-todo-marker"]
        assert len(todo_findings) >= 1

    def test_skip_internal_discovery_env(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")

        with patch.dict(os.environ, {"--skip-internal-discovery": "1"}):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file)

        assert findings == []

    def test_inventory_invalid_quantity_detected(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        inv = [f for f in findings if f.rule == "inventory-invalid-quantity"]
        assert len(inv) == 1

    def test_docs_quickstart_gap_not_triggered_when_install_present(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        quickstart = [f for f in findings if f.rule == "docs-quickstart-gap"]
        assert len(quickstart) == 0

    def test_docs_quickstart_gap_triggered_when_install_missing(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        (repo / "README.md").write_text("# Project\npytest -q\n")
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        quickstart = [f for f in findings if f.rule == "docs-quickstart-gap"]
        assert len(quickstart) == 1

    def test_test_gap_missing_file_detected(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        tests_dir = repo / "tests"
        if (tests_dir / "test_notifications.py").exists():
            (tests_dir / "test_notifications.py").unlink()
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        gap = [f for f in findings if f.rule == "test-gap-missing-file"]
        assert len(gap) == 1
        assert gap[0].path == "tests/test_notifications.py"

    def test_trailing_whitespace_detected(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        (repo / "price.py").write_text(
            "def calculate_discount(amount, discount):\n    return amount + discount   \n"
        )
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        tw = [f for f in findings if f.rule == "trailing-whitespace"]
        assert len(tw) >= 1
        assert any(f.path == "price.py" for f in tw)

    def test_external_tools_findings_appended(self, tmp_path, make_finding):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        fake_ruff = [
            _mf(
                make_finding,
                rule="ruff-e501",
                path="src/main.py",
                line=42,
                confidence=0.65,
            )
        ]
        fake_xo = [
            _mf(
                make_finding,
                rule="xo-max-lines",
                path="src/big.ts",
                line=1,
                confidence=0.85,
            )
        ]
        fake_type = [
            _mf(
                make_finding,
                rule="type-explicit-any",
                path="src/api.ts",
                line=10,
                confidence=0.85,
            )
        ]
        fake_cov = [
            _mf(
                make_finding,
                rule="test-coverage-branch",
                path="src/app.py",
                line=20,
                confidence=0.82,
            )
        ]

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=fake_type,
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=fake_cov,
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings",
                return_value=fake_xo,
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=fake_ruff,
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        rules = {f.rule for f in findings}
        assert "ruff-e501" in rules
        assert "xo-max-lines" in rules
        assert "type-explicit-any" in rules
        assert "test-coverage-branch" in rules

    def test_empty_repo_only_test_gap_findings(self, tmp_path):
        repo = tmp_path / "empty-repo"
        repo.mkdir()
        (repo / "tests").mkdir()
        (repo / "tests" / "test_notifications.py").write_text("pass\n")
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        with (
            patch("bluei.engine.orchestrator._ast_scan_python_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        assert findings == []


# ────────────────────────────────────────────────────────────────
# 2. Cross-source deduplication
# ────────────────────────────────────────────────────────────────


class TestCrossSourceDeduplication:
    def test_ast_finding_deduped_against_string_finding(self, tmp_path):
        """Create a file where both string detector and AST scanner would find
        the same issue at the same (path, line, rule). Only one survives."""
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        ast_finding = Finding(
            finding_id="ast-dupe-001",
            repo=str(repo),
            path="price.py",
            line=2,
            rule="discount-math-sign",
            snippet="return amount + discount",
            confidence=0.95,
            quick_win=True,
            safe_to_autofix=True,
        )

        with (
            patch(
                "bluei.engine.orchestrator._ast_scan_python_files",
                return_value=[ast_finding],
            ),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        discount = [f for f in findings if f.rule == "discount-math-sign"]
        assert len(discount) == 1

    def test_ast_finding_on_different_line_not_deduped(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        ast_finding = Finding(
            finding_id="ast-new-line-001",
            repo=str(repo),
            path="price.py",
            line=99,
            rule="discount-math-sign",
            snippet="return amount + discount",
            confidence=0.95,
            quick_win=True,
            safe_to_autofix=True,
        )

        with (
            patch(
                "bluei.engine.orchestrator._ast_scan_python_files",
                return_value=[ast_finding],
            ),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        discount = [f for f in findings if f.rule == "discount-math-sign"]
        assert len(discount) == 1

    def test_ast_findings_without_string_counterpart_are_added(self, tmp_path):
        repo = _build_standard_repo(tmp_path)
        log_file = tmp_path / "run.log"
        log_file.write_text("")
        docs_index = tmp_path / "docs_index.json"
        docs_index.write_text("[]")

        ast_finding = Finding(
            finding_id="ast-unique-001",
            repo=str(repo),
            path="src/qa_sandbox/utils.py",
            line=10,
            rule="broad-except",
            snippet="except Exception:",
            confidence=0.88,
            quick_win=False,
            safe_to_autofix=False,
        )

        with (
            patch(
                "bluei.engine.orchestrator._ast_scan_python_files",
                return_value=[ast_finding],
            ),
            patch("bluei.engine.orchestrator._ast_scan_ts_js_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_go_files", return_value=[]),
            patch("bluei.engine.orchestrator._ast_scan_rust_files", return_value=[]),
            patch(
                "bluei.engine.orchestrator.discover_typescript_type_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_test_coverage_findings",
                return_value=[],
            ),
            patch(
                "bluei.engine.orchestrator.discover_xo_linter_findings", return_value=[]
            ),
            patch(
                "bluei.engine.orchestrator.discover_python_linter_findings",
                return_value=[],
            ),
            patch("bluei.engine.orchestrator.run_plugin_discovery", return_value=[]),
            patch("bluei.engine.orchestrator.load_docs_index", return_value=[]),
        ):
            from bluei.engine.orchestrator import discover_findings

            findings = discover_findings(repo, log_file, docs_index)

        broad = [f for f in findings if f.rule == "broad-except"]
        utils_broad = [f for f in broad if f.path == "src/qa_sandbox/utils.py"]
        assert len(utils_broad) == 1


# ────────────────────────────────────────────────────────────────
# 3. Command builders
# ────────────────────────────────────────────────────────────────


class TestBuildPrCycleCommand:
    def test_contains_run_phase_pr_cycle(self, tmp_path):
        from bluei.engine.orchestrator import build_pr_cycle_command

        args = _build_mock_args()
        cmd = build_pr_cycle_command(args)
        assert "--run-phase" in cmd
        assert "pr-cycle" in cmd

    def test_contains_no_dry_run(self, tmp_path):
        from bluei.engine.orchestrator import build_pr_cycle_command

        args = _build_mock_args()
        cmd = build_pr_cycle_command(args)
        assert "--no-dry-run" in cmd

    def test_includes_repo_path(self, tmp_path):
        from bluei.engine.orchestrator import build_pr_cycle_command

        args = _build_mock_args(repo_path=Path("/my/repo"))
        cmd = build_pr_cycle_command(args)
        assert "/my/repo" in cmd


class TestBuildMergeCycleCommand:
    def test_contains_run_phase_merge_cycle(self, tmp_path):
        from bluei.engine.orchestrator import build_merge_cycle_command

        args = _build_mock_args()
        cmd = build_merge_cycle_command(args)
        assert "--run-phase" in cmd
        assert "merge-cycle" in cmd

    def test_contains_auto_merge_sandbox(self, tmp_path):
        from bluei.engine.orchestrator import build_merge_cycle_command

        args = _build_mock_args()
        cmd = build_merge_cycle_command(args)
        assert "--auto-merge-sandbox" in cmd

    def test_contains_no_dry_run(self, tmp_path):
        from bluei.engine.orchestrator import build_merge_cycle_command

        args = _build_mock_args()
        cmd = build_merge_cycle_command(args)
        assert "--no-dry-run" in cmd


class TestBuildDocsIndexRefreshCommand:
    def test_contains_refresh_docs_index(self, tmp_path):
        from bluei.engine.orchestrator import build_docs_index_refresh_command

        args = _build_mock_args()
        cmd = build_docs_index_refresh_command(args)
        assert "--refresh-docs-index" in cmd

    def test_contains_run_phase_docs_index(self, tmp_path):
        from bluei.engine.orchestrator import build_docs_index_refresh_command

        args = _build_mock_args()
        cmd = build_docs_index_refresh_command(args)
        assert "--run-phase" in cmd
        assert "docs-index" in cmd

    def test_includes_docs_index_file_path(self, tmp_path):
        from bluei.engine.orchestrator import build_docs_index_refresh_command

        args = _build_mock_args(docs_index_file=Path("/custom/docs.json"))
        cmd = build_docs_index_refresh_command(args)
        assert "/custom/docs.json" in cmd


class TestBuildActiveCycleCommand:
    def test_uses_args_run_phase(self, tmp_path):
        from bluei.engine.orchestrator import build_active_cycle_command

        args = _build_mock_args(run_phase="active-cycle")
        cmd = build_active_cycle_command(args)
        assert "active-cycle" in cmd
        assert "--no-dry-run" in cmd


class TestBuildRefactorCycleCommand:
    def test_includes_max_queue_items(self, tmp_path):
        from bluei.engine.orchestrator import build_refactor_cycle_command

        args = _build_mock_args(max_queue_items=5)
        cmd = build_refactor_cycle_command(args)
        assert "--max-queue-items" in cmd
        assert "5" in cmd

    def test_auto_approve_flag(self, tmp_path):
        from bluei.engine.orchestrator import build_refactor_cycle_command

        args = _build_mock_args(auto_approve=True)
        cmd = build_refactor_cycle_command(args)
        assert "--auto-approve" in cmd


class TestBuildReconcileOnlyCommand:
    def test_contains_reconcile_only(self, tmp_path):
        from bluei.engine.orchestrator import build_reconcile_only_command

        args = _build_mock_args()
        cmd = build_reconcile_only_command(args)
        assert "--reconcile-only" in cmd


# ────────────────────────────────────────────────────────────────
# 4. Issue lifecycle helpers
# ────────────────────────────────────────────────────────────────


class TestCreateIssuesForFindings:
    def test_creates_issue_for_qualifying_finding(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        finding = _mf(make_finding, confidence=0.95)
        issues_data = {"issues": []}
        created = create_issues_for_findings(issues_data, [finding], 0.8, 10)
        assert len(created) == 1
        assert created[0]["status"] == "open"
        assert created[0]["finding_id"] == finding.finding_id

    def test_filters_below_confidence_threshold(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        finding = _mf(make_finding, confidence=0.5)
        issues_data = {"issues": []}
        created = create_issues_for_findings(issues_data, [finding], 0.8, 10)
        assert len(created) == 0

    def test_respects_max_issues_per_run_cap(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        findings = [_mf(make_finding, line=i, confidence=0.9) for i in range(10)]
        issues_data = {"issues": []}
        created = create_issues_for_findings(issues_data, findings, 0.8, 3)
        assert len(created) == 3

    def test_skips_duplicate_finding_id(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        finding = _mf(make_finding, confidence=0.95)
        issues_data = {"issues": [{"finding_id": finding.finding_id, "status": "open"}]}
        created = create_issues_for_findings(issues_data, [finding], 0.8, 10)
        assert len(created) == 0

    def test_issues_appended_to_issues_data(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import create_issues_for_findings

        finding = _mf(make_finding, confidence=0.95)
        issues_data = {"issues": []}
        create_issues_for_findings(issues_data, [finding], 0.8, 10)
        assert len(issues_data["issues"]) == 1

    def test_mixed_confidence_only_qualifying_create_issues(
        self, tmp_path, make_finding
    ):
        from bluei.engine.orchestrator import create_issues_for_findings

        high = _mf(make_finding, line=1, confidence=0.95)
        low = _mf(make_finding, line=2, confidence=0.5)
        issues_data = {"issues": []}
        created = create_issues_for_findings(issues_data, [high, low], 0.8, 10)
        assert len(created) == 1
        assert created[0]["finding_id"] == high.finding_id


class TestChooseSafeAutofixItems:
    def test_only_safe_and_above_threshold(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import choose_safe_autofix_items

        safe_high = _mf(make_finding, line=1, confidence=0.95, safe_to_autofix=True)
        safe_low = _mf(make_finding, line=2, confidence=0.5, safe_to_autofix=True)
        unsafe_high = _mf(make_finding, line=3, confidence=0.95, safe_to_autofix=False)
        result = choose_safe_autofix_items([safe_high, safe_low, unsafe_high], 0.8)
        assert len(result) == 1
        assert result[0].finding_id == safe_high.finding_id

    def test_empty_list_returns_empty(self, tmp_path):
        from bluei.engine.orchestrator import choose_safe_autofix_items

        assert choose_safe_autofix_items([], 0.8) == []

    def test_exactly_at_threshold_passes(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import choose_safe_autofix_items

        exact = _mf(make_finding, line=1, confidence=0.80, safe_to_autofix=True)
        result = choose_safe_autofix_items([exact], 0.8)
        assert len(result) == 1

    def test_just_below_threshold_excluded(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import choose_safe_autofix_items

        below = _mf(make_finding, line=1, confidence=0.79, safe_to_autofix=True)
        result = choose_safe_autofix_items([below], 0.8)
        assert len(result) == 0


class TestCheckConsecutiveFixFailures:
    def test_three_consecutive_failures(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is True

    def test_two_failures_not_enough(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is False

    def test_success_resets_counter(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "pr_opened", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is False

    def test_open_resets_counter(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is False

    def test_empty_history(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        assert check_consecutive_fix_failures({"history": []}, 3) is False

    def test_no_history_key(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        assert check_consecutive_fix_failures({}, 3) is False

    def test_needs_human_events_count_as_failures(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "needs-human-validation-failed", "at": now_iso()},
                {"event": "needs-human-scope-limit-exceeded", "at": now_iso()},
                {"event": "needs-human-commit-failed", "at": now_iso()},
            ]
        }
        assert check_consecutive_fix_failures(issue, 3) is True

    def test_custom_threshold(self, tmp_path):
        from bluei.engine.orchestrator import check_consecutive_fix_failures

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ]
        }
        assert check_consecutive_fix_failures(issue, 2) is True
        assert check_consecutive_fix_failures(issue, 3) is False


class TestCheckFindingEscalationBeforeFix:
    def test_escalates_on_consecutive_failures(self, tmp_path):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ],
            "finding_id": "abc",
            "issue_id": "QA-0001",
            "rule": "discount-math-sign",
        }
        assert check_finding_escalation_before_fix(issue, {}, 3) is True

    def test_no_escalation_without_failures(self, tmp_path):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
            ],
            "finding_id": "abc",
            "issue_id": "QA-0001",
            "rule": "discount-math-sign",
        }
        assert check_finding_escalation_before_fix(issue, {}, 3) is False

    def test_writes_to_log_file(self, tmp_path):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        log_file = tmp_path / "escalation.log"
        log_file.write_text("")
        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ],
            "finding_id": "abc",
            "issue_id": "QA-0001",
            "rule": "discount-math-sign",
        }
        result = check_finding_escalation_before_fix(issue, {}, 3, log_file=log_file)
        assert result is True
        log_text = log_file.read_text()
        assert "escalation:" in log_text
        assert "QA-0001" in log_text

    def test_resolved_verified_resets_escalation(self, tmp_path):
        from bluei.engine.orchestrator import check_finding_escalation_before_fix

        issue = {
            "history": [
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "resolved_verified", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ],
            "finding_id": "abc",
            "issue_id": "QA-0001",
            "rule": "discount-math-sign",
        }
        assert check_finding_escalation_before_fix(issue, {}, 3) is False


class TestCountFailedFixAttempts:
    def test_counts_failures_after_last_open(self, tmp_path):
        from bluei.engine.orchestrator import count_failed_fix_attempts

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
                {"event": "needs-human-push-failed", "at": now_iso()},
                {"event": "open", "at": now_iso()},
                {"event": "fix_failed_verification", "at": now_iso()},
            ]
        }
        assert count_failed_fix_attempts(issue) == 1

    def test_zero_failures(self, tmp_path):
        from bluei.engine.orchestrator import count_failed_fix_attempts

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
            ]
        }
        assert count_failed_fix_attempts(issue) == 0

    def test_counts_needs_human_variants(self, tmp_path):
        from bluei.engine.orchestrator import count_failed_fix_attempts

        issue = {
            "history": [
                {"event": "open", "at": now_iso()},
                {"event": "needs-human-validation-failed", "at": now_iso()},
                {"event": "needs-human-max-retries-exceeded", "at": now_iso()},
            ]
        }
        assert count_failed_fix_attempts(issue) == 2


class TestEnsureIssueForFinding:
    def test_returns_existing_issue(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import ensure_issue_for_finding

        finding = _mf(make_finding, confidence=0.95)
        existing = {"finding_id": finding.finding_id, "status": "open"}
        issues_data = {"issues": [existing]}
        result = ensure_issue_for_finding(issues_data, finding, 0.8)
        assert result is existing

    def test_creates_new_issue_when_qualifying(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import ensure_issue_for_finding

        finding = _mf(make_finding, confidence=0.95)
        issues_data = {"issues": []}
        result = ensure_issue_for_finding(issues_data, finding, 0.8)
        assert result is not None
        assert result["status"] == "open"
        assert len(issues_data["issues"]) == 1

    def test_returns_none_below_threshold(self, tmp_path, make_finding):
        from bluei.engine.orchestrator import ensure_issue_for_finding

        finding = _mf(make_finding, confidence=0.5)
        issues_data = {"issues": []}
        result = ensure_issue_for_finding(issues_data, finding, 0.8)
        assert result is None
        assert len(issues_data["issues"]) == 0
