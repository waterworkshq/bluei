"""A current projection replaces interruption metadata only with lifecycle evidence."""
import json

import pytest

from bluei.engine.commands.helpers import run_mode_label, update_status_artifact
from tests.test_cli_guard import _make_full_args


@pytest.mark.parametrize("completed", [True, False, None])
def test_current_projection_replaces_prior_interruption(tmp_path, completed):
    status = tmp_path / "status.json"
    issues = tmp_path / "issues.json"
    findings = tmp_path / "findings.jsonl"
    prior = {"phase": "issue-cycle", "error": "prior failure"}
    status.write_text(json.dumps({"run_interruption": prior, "last_run_completed": False}))
    issues.write_text('{"issues": []}')
    findings.write_text("")
    metrics = {"run_id": "current"}
    if completed is not None:
        metrics["run_completed"] = completed
    update_status_artifact(
        status_file=status, state={}, issues_file=issues, findings_file=findings,
        args=_make_full_args(), run_mode="ACTIVE-SIM-ORCHESTRATED",
        reconcile_event={}, run_metrics=metrics,
    )
    result = json.loads(status.read_text())
    assert result["latest_run_metrics"]["run_id"] == "current"
    if completed is None:
        assert result["run_interruption"] == prior
        assert result["last_run_completed"] is False
    else:
        assert "run_interruption" not in result
        assert result["last_run_completed"] is completed


@pytest.mark.parametrize("live,dry,expected", [
    (False, True, "DRY-RUN"), (False, False, "ACTIVE-SIM"),
    (True, True, "DRY-RUN-LIVE"), (True, False, "ACTIVE-LIVE"),
])
def test_shared_mode_label_preserves_existing_modes(live, dry, expected):
    assert run_mode_label(_make_full_args(live_github_actions=live, dry_run=dry)) == expected + "-ORCHESTRATED"
