"""Closure evidence must come from a detector that can report the rule.

Real-outcome consumer regressions for the PR11 review fix: a coverage finding
must not close because an unrelated detector ran, verify-only must leave such
issues untouched, unknown rules stay open, and a completed responsible ruff
detector still closes a real Ruff F821 finding.
"""

import shutil
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bluei.engine.commands.verify_only import run_verify_only
from bluei.engine.discovery import discover_findings
from bluei.engine.models import Finding
from bluei.engine.validation import verify_fix_closed

have_ruff = shutil.which("ruff") is not None


def _python_repo(tmp_path):
    """Minimal python repo: string/AST detectors and ruff apply, coverage skips."""
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text("value = 1\n")
    return tmp_path


def _real_outcomes(repo):
    outcomes = {}
    discover_findings(
        repo,
        log_file=repo / "run.log",
        docs_index_file=repo / "docs-index.json",
        outcomes=outcomes,
    )
    return outcomes


def _args():
    return SimpleNamespace(dry_run=False, live_github_actions=False,
                           workspace=None, repo_path="/tmp/repo")


def _run_verify_only(tmp_path, findings, issues, outcomes):
    captured = StringIO()
    with (
        patch("bluei.engine.commands.verify_only.save_issues"),
        patch("bluei.engine.commands.verify_only.save_state"),
        patch("bluei.engine.commands.verify_only.update_status_artifact"),
        patch("bluei.engine.orchestrator.set_issue_status") as mock_sis,
        patch("sys.stdout", captured),
    ):
        rc = run_verify_only(
            findings=findings,
            issues_data={"issues": issues},
            issues_file=tmp_path / "issues.json",
            state_file=tmp_path / "state.json",
            state={},
            status_file=tmp_path / "status.json",
            findings_file=tmp_path / "findings.json",
            log_file=tmp_path / "test.log",
            args=_args(),
            reconcile_event={"reason": "test"},
            previous_last_run_at=None,
            open_issues=len(issues),
            open_prs=0,
            written_findings=len(findings),
            suppressed_findings=[],
            blocked_reasons=[],
            detector_outcomes=outcomes,
        )
    return rc, mock_sis, captured.getvalue()


def test_coverage_finding_stays_open_when_coverage_detector_skipped(tmp_path):
    repo = _python_repo(tmp_path)
    outcomes = _real_outcomes(repo)
    assert outcomes["test-coverage"]["status"] == "skipped"
    assert any(r["status"] == "ran" for r in outcomes.values())  # unrelated ran
    finding = Finding("cov1", str(repo), "app.py", 1, "test-coverage-branch",
                      "Branch not covered", 0.82, False, False)
    log = repo / "verify.log"
    assert not verify_fix_closed(repo, finding, log,
                                 docs_index_file=repo / "docs-index.json")
    assert "incomplete discovery" in log.read_text()


def test_unknown_rule_stays_open_even_when_detectors_ran(tmp_path):
    repo = _python_repo(tmp_path)
    outcomes = _real_outcomes(repo)
    assert any(r["status"] == "ran" for r in outcomes.values())
    finding = Finding("u1", str(repo), "app.py", 1, "learned-custom-x1",
                      "arbitrary learned rule", 0.5, False, False)
    assert not verify_fix_closed(repo, finding, repo / "verify.log",
                                 docs_index_file=repo / "docs-index.json")


def test_doc_gap_stays_open_without_explicit_index_outcome(tmp_path):
    # docs-index scan has no dedicated outcome key; ast detectors running
    # (and string detectors running or skipping) is not evidence about it.
    repo = _python_repo(tmp_path)
    outcomes = _real_outcomes(repo)
    assert outcomes["ast-python"]["status"] == "ran"
    finding = Finding("d1", str(repo), "app.py", 1, "doc-gap-uncovered-module",
                      "missing docs coverage", 0.78, False, False)
    assert not verify_fix_closed(repo, finding, repo / "verify.log",
                                 docs_index_file=repo / "docs-index.json")


def test_known_ast_rule_closes_when_ast_python_ran(tmp_path):
    repo = _python_repo(tmp_path)
    outcomes = _real_outcomes(repo)
    assert outcomes["ast-python"]["status"] == "ran"
    finding = Finding("a1", str(repo), "app.py", 1, "perf-pop-front-loop",
                      "list.pop(0) in loop", 0.83, False, False)
    assert verify_fix_closed(repo, finding, repo / "verify.log",
                             docs_index_file=repo / "docs-index.json")


@pytest.mark.skipif(not have_ruff, reason="ruff not installed")
def test_real_ruff_f821_closes_on_completed_responsible_detector(tmp_path):
    repo = _python_repo(tmp_path)
    (repo / "app.py").write_text("print('resolved')\n")
    finding = Finding("f1", str(repo), "app.py", 1, "ruff-F821",
                      "Undefined name `missing_name`", 0.9, False, False)
    assert verify_fix_closed(repo, finding, repo / "verify.log",
                             docs_index_file=repo / "docs-index.json")


def test_uncatalogued_ruff_code_untouched_when_only_core_ruff_ran(tmp_path):
    # Core python-linter-ruff filters codes absent from detector_catalog
    # (F821): only plugin-python can report it, so core running is no evidence.
    issues = [{"issue_id": "f", "rule": "ruff-F821", "path": "app.py",
               "status": "open"}]
    rc, mock_sis, out = _run_verify_only(
        tmp_path, [], issues,
        {"python-linter-ruff": {"status": "ran", "count": 0}},
    )
    assert rc == 0
    assert mock_sis.call_count == 0
    assert issues[0]["status"] == "open"
    assert "fixes_untouched=1" in out


@pytest.mark.skipif(not have_ruff, reason="ruff not installed")
def test_verify_only_splits_resolved_and_untouched_per_responsible_detector(tmp_path):
    repo = _python_repo(tmp_path)
    outcomes = _real_outcomes(repo)
    assert outcomes["test-coverage"]["status"] == "skipped"
    assert outcomes["python-linter-ruff"]["status"] == "ran"
    issues = [
        {"issue_id": "cov", "rule": "test-coverage-branch", "path": "app.py",
         "status": "open"},
        {"issue_id": "lint", "rule": "ruff-e501", "path": "other.py",
         "status": "open"},
    ]
    rc, mock_sis, out = _run_verify_only(tmp_path, [], issues, outcomes)
    assert rc == 0  # scan itself completed; honesty is per-issue
    verified = [c for c in mock_sis.call_args_list
                if c[0][1] == "resolved_verified"]
    failed = [c for c in mock_sis.call_args_list
              if c[0][1] == "fix_failed_verification"]
    assert len(verified) == 1 and verified[0][0][0] is issues[1]
    assert not failed
    assert issues[0]["status"] == "open"  # untouched, not resolved
    assert "fixes_untouched=1" in out and "fixes_verified=1" in out


def test_verify_only_leaves_unknown_rule_untouched(tmp_path):
    issues = [{"issue_id": "u", "rule": "learned-custom-x1", "path": "a.py",
               "status": "open"}]
    rc, mock_sis, out = _run_verify_only(
        tmp_path, [], issues,
        {"python-linter-ruff": {"status": "ran", "count": 0}},
    )
    assert rc == 0
    assert mock_sis.call_count == 0  # neither resolved nor failed: no evidence
    assert issues[0]["status"] == "open"
    assert "fixes_untouched=1" in out


def test_verify_only_resolves_bare_markdown_code_when_markdown_plugin_ran(tmp_path):
    # markdownlint emits bare codes ("MD009"); the markdown plugin is producer.
    issues = [{"issue_id": "m", "rule": "MD009", "path": "README.md",
               "status": "open"}]
    rc, mock_sis, _ = _run_verify_only(
        tmp_path, [], issues,
        {"plugin:plugin-markdown": {"status": "ran", "count": 0}},
    )
    assert rc == 0
    assert [c for c in mock_sis.call_args_list
            if c[0][1] == "resolved_verified"]
