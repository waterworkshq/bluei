"""Scoped command guards: opencode template requirement, no_learning lesson
gate, and python-scoped status command fields (round-trip validated)."""

import json
import shlex
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.engine.commands.finalize import run_finalize_phase
from bluei.engine.commands.helpers import update_status_artifact
from bluei.engine.commands.parse_args import parse_args
from bluei.engine.constants import DEFAULT_CLAUDE_CMD_TEMPLATE


@pytest.fixture(autouse=True)
def _skip_escalation_analysis():
    with patch("bluei.engine.escalation.run_escalation_checks", return_value=[]):
        yield


def _parse(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bluei", *argv])
    return parse_args()


# ── opencode requires an explicit template (no wrong-backend default) ──


def test_opencode_without_template_rejected(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _parse(["--fix-engine", "opencode"], monkeypatch)
    err = capsys.readouterr().err
    assert "requires --claude-cmd-template" in err


def test_discovery_only_opencode_does_not_require_template(monkeypatch):
    args = _parse(["--fix-engine", "opencode", "--run-phase", "issue-cycle"], monkeypatch)
    assert args.fix_engine == "opencode" and args.run_phase == "issue-cycle"


def test_opencode_with_template_accepted(monkeypatch):
    args = _parse(
        ["--fix-engine", "opencode", "--claude-cmd-template", "opencode run {prompt}"],
        monkeypatch,
    )
    assert args.fix_engine == "opencode"
    assert args.claude_cmd_template == "opencode run {prompt}"


def test_claude_default_template_still_applied(monkeypatch):
    args = _parse([], monkeypatch)
    assert args.claude_cmd_template == DEFAULT_CLAUDE_CMD_TEMPLATE


# ── no_learning gates the finalize lesson WRITE ──


def _cost_tracker():
    ct = MagicMock()
    ct.cycle_total.return_value = 0.05
    ct.cycle_savings.return_value = 0.0
    ct.warned.return_value = False
    ct.exceeded_limit.return_value = False
    return ct


def _finalize_kwargs(tmp_path, **overrides):
    defaults = dict(
        state_file=tmp_path / "state.json",
        issues_file=tmp_path / "issues.json",
        status_file=tmp_path / "status.json",
        findings_file=tmp_path / "findings.json",
        log_file=tmp_path / "test.log",
        lessons_file=tmp_path / "lessons.md",
        repo_path=tmp_path,
        args=SimpleNamespace(
            run_phase="issue-cycle",
            dry_run=True,
            live_github_actions=False,
            reconcile_only=False,
            max_fix_attempts_per_issue=3,
            max_duplicate_prs_threshold=3,
            no_auto_close_duplicate_prs=False,
            no_learning=True,
        ),
        state={},
        issues_data={"issues": []},
        reconcile_event={},
        previous_last_run_at=None,
        open_issues=0,
        open_prs=0,
        findings=[],
        written_findings=0,
        created_issues=[],
        suppressed_findings=[],
        blocked_reasons=[],
        fix_attempts=0,
        fixes_verified=0,
        # lesson content trigger: something broke this cycle
        fixes_failed_verification=1,
        created_prs=0,
        issues_escalated_max_retries=0,
        merge_attempts=0,
        merges_succeeded=0,
        merges_failed=0,
        merged_pr_urls=[],
        claude_invocations=0,
        opencode_invocations=0,
        deterministic_invocations=0,
        cost_tracker=_cost_tracker(),
        cost_log_path=tmp_path / "cost.json",
        gh_repo_slug="acme/widget",
        ledger_records=[],
        pattern_store=None,
    )
    defaults.update(overrides)
    return defaults


def _run_finalize(tmp_path, **overrides):
    kwargs = _finalize_kwargs(tmp_path, **overrides)
    with patch("bluei.engine.commands.finalize._append_text"), patch(
        "bluei.engine.commands.finalize.update_status_artifact"
    ), patch("bluei.engine.commands.finalize.save_state"), patch(
        "bluei.engine.commands.finalize.save_issues"
    ), patch("sys.stdout", StringIO()):
        return run_finalize_phase(**kwargs)


def test_no_learning_skips_append_lesson(tmp_path):
    with patch("bluei.engine.commands.finalize.append_lesson") as spy:
        _run_finalize(tmp_path)
        spy.assert_not_called()


def test_learning_enabled_appends_lesson(tmp_path):
    kwargs = _finalize_kwargs(tmp_path)
    kwargs["args"].no_learning = False  # explicit: learning writes the lesson
    with patch("bluei.engine.commands.finalize.append_lesson") as spy, patch(
        "bluei.engine.commands.finalize._append_text"
    ), patch("bluei.engine.commands.finalize.update_status_artifact"), patch(
        "bluei.engine.commands.finalize.save_state"
    ), patch("bluei.engine.commands.finalize.save_issues"), patch(
        "sys.stdout", StringIO()
    ):
        run_finalize_phase(**kwargs)
        spy.assert_called_once()


# ── python-scoped status: only valid command fields, round-trip parsed ──


def _write_status(args, tmp_path):
    status_file = tmp_path / "status.json"
    update_status_artifact(
        status_file=status_file,
        state={},
        issues_file=tmp_path / "missing-issues.json",
        findings_file=tmp_path / "missing-findings.json",
        args=args,
        run_mode="test",
        reconcile_event={},
    )
    return json.loads(status_file.read_text())


def test_scoped_status_plain_discovery_only_manual(tmp_path, monkeypatch):
    args = _parse(
        ["--discovery-scope", "python", "--run-phase", "issue-cycle"], monkeypatch
    )
    status = _write_status(args, tmp_path)

    assert "manual_one_cycle_command" in status
    assert "pr_cycle_command" not in status
    assert "merge_cycle_command" not in status
    assert "orchestrated_cycle_command" not in status
    assert "refactor_cycle_command" not in status
    assert "reconcile_only_command" not in status
    assert "verification_only_command" not in status
    assert "docs_index_refresh_command" not in status
    # Plain discovery: no --finding-id selector in the emitted command.
    assert "--finding-id" not in status["manual_one_cycle_command"]

    # Round-trip: the emitted command must itself parse. The built command
    # starts "python <cli.py> --repo ...", so drop the interpreter+script.
    roundtrip = _parse(shlex.split(status["manual_one_cycle_command"])[2:], monkeypatch)
    assert roundtrip.discovery_scope == "python"
    assert roundtrip.run_phase == "issue-cycle"


def test_scoped_status_orchestrated_emits_orchestrated(tmp_path, monkeypatch):
    args = _parse(
        [
            "--discovery-scope", "python",
            "--finding-id", "f-1",
            "--run-phase", "orchestrated",
            "--fix-engine", "claude",
        ],
        monkeypatch,
    )
    status = _write_status(args, tmp_path)
    assert "manual_one_cycle_command" in status
    assert "orchestrated_cycle_command" in status
    assert "pr_cycle_command" not in status
    assert "--finding-id" in status["orchestrated_cycle_command"]

    roundtrip = _parse(
        shlex.split(status["orchestrated_cycle_command"])[2:], monkeypatch
    )
    assert roundtrip.discovery_scope == "python"
    assert roundtrip.finding_id == "f-1"


def test_scoped_status_removes_stale_unsupported_fields(tmp_path, monkeypatch):
    # A prior repository-scope run may have persisted pr/merge/etc commands;
    # the python-scope overwrite must remove them, not keep them around.
    args = _parse(
        ["--discovery-scope", "python", "--run-phase", "issue-cycle"], monkeypatch
    )
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "pr_cycle_command": "bluei --run-phase pr-cycle",
                "merge_cycle_command": "bluei --run-phase merge-cycle",
                "docs_index_refresh_command": "bluei --run-phase docs-index-refresh",
            }
        )
    )
    update_status_artifact(
        status_file=status_file,
        state={},
        issues_file=tmp_path / "missing-issues.json",
        findings_file=tmp_path / "missing-findings.json",
        args=args,
        run_mode="test",
        reconcile_event={},
    )
    status = json.loads(status_file.read_text())
    assert "pr_cycle_command" not in status
    assert "merge_cycle_command" not in status
    assert "docs_index_refresh_command" not in status
    assert "manual_one_cycle_command" in status


def test_default_scope_status_keeps_all_commands(tmp_path, monkeypatch):
    args = _parse([], monkeypatch)
    status = _write_status(args, tmp_path)
    for key in (
        "manual_one_cycle_command",
        "issue_cycle_command",
        "pr_cycle_command",
        "merge_cycle_command",
        "orchestrated_cycle_command",
        "refactor_cycle_command",
        "reconcile_only_command",
        "verification_only_command",
        "docs_index_refresh_command",
    ):
        assert key in status, key


# ── opencode is rejected for refactor-cycle (phase hardcodes Claude) ──


def test_opencode_refactor_cycle_rejected_even_with_template(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _parse(
            [
                "--fix-engine", "opencode",
                "--run-phase", "refactor-cycle",
                "--claude-cmd-template", "opencode run {prompt}",
            ],
            monkeypatch,
        )
    err = capsys.readouterr().err
    assert "not supported for refactor-cycle" in err


def test_default_engine_refactor_cycle_still_accepted(monkeypatch):
    args = _parse(["--run-phase", "refactor-cycle"], monkeypatch)
    assert args.run_phase == "refactor-cycle"
    assert args.fix_engine != "opencode"
