"""Selection must use fresh discovery and never route another finding."""
import json
from types import SimpleNamespace

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.discover import run_discover_phase


@pytest.mark.parametrize("selected", [None, "raw-e722"])
def test_scoped_raw_e722_is_recorded_but_not_actionable(
    tmp_path, monkeypatch, make_finding, selected
):
    import bluei.engine.orchestrator as orchestrator

    raw = make_finding(finding_id="raw-e722", rule="ruff-E722", path=".venv/extra.py")
    supported = make_finding(finding_id="f821", rule="ruff-F821", path="app.py")

    def discover(repo, outcomes=None, **kwargs):
        outcomes["plugin:plugin-python"] = {"status": "ran", "count": 2}
        return [raw, supported]

    monkeypatch.setattr(orchestrator, "discover_findings", discover)
    args = SimpleNamespace(
        run_phase="orchestrated", open_issues_cap=1,
        finding_cooldown_seconds=0, live_github_actions=False,
        issue_confidence_threshold=0, discovery_scope="python", finding_id=selected,
        dry_run=False,
    )
    ctx = RunContext(
        args=args, repo_path=tmp_path, log_file=tmp_path / "run.log",
        findings_file=tmp_path / "findings.jsonl", docs_index_file=tmp_path / "docs.json",
        status_file=tmp_path / "status.json", run_id="fresh-run", run_issue_cycle=True,
        run_pr_cycle=True, issues_data={"issues": []},
    )
    result = run_discover_phase(ctx)
    assert [f.finding_id for f in result[0]] == ["raw-e722", "f821"]
    recorded = [json.loads(line) for line in ctx.findings_file.read_text().splitlines()]
    assert any(f["finding_id"] == "raw-e722" and f["rule"] == "ruff-E722" for f in recorded)
    assert [f.finding_id for f in result[2]] == ([] if selected else ["f821"])
    assert result[4] == [] and result[6]
    assert ctx.issues_data == {"issues": []}
    assert "non-actionable" in ctx.log_file.read_text()
    if selected:
        assert any("unsupported" in reason for reason in result[5])


@pytest.mark.parametrize("status,selected,present,duplicates,expected", [
    ("ran", "f-selected", True, False, ["f-selected"]),
    ("failed", "f-selected", True, False, []),
    ("unavailable", "f-selected", True, False, []),
    ("skipped", "f-selected", True, False, []),
    ("ran", "absent", True, False, []),
    ("ran", "f-selected", False, False, []),
    # Two exact copies of the selected ID: not uniquely rediscovered, so
    # nothing is eligible and the run must fail closed.
    ("ran", "f-selected", True, True, []),
])
def test_selected_finding_requires_fresh_responsible_producer(
    tmp_path, monkeypatch, make_finding, status, selected, present, duplicates, expected
):
    import bluei.engine.orchestrator as orchestrator

    actual = make_finding(finding_id="f-selected", rule="ruff-F821", path="app.py")
    other = make_finding(finding_id="f-other", rule="ruff-F821", path="other.py")

    def discover(repo, outcomes=None, **kwargs):
        assert kwargs["discovery_scope"] == "python"
        outcomes["plugin:plugin-python"] = {"status": status, "count": 2}
        if not present:
            return [other]
        return [actual, other, actual] if duplicates else [actual, other]

    monkeypatch.setattr(orchestrator, "discover_findings", discover)
    args = SimpleNamespace(run_phase="orchestrated", open_issues_cap=20,
                           finding_cooldown_seconds=0, live_github_actions=False,
                           issue_confidence_threshold=0, discovery_scope="python", finding_id=selected,
                           dry_run=False)
    ctx = RunContext(args=args, repo_path=tmp_path, log_file=tmp_path / "run.log",
                     findings_file=tmp_path / "findings.jsonl", docs_index_file=tmp_path / "docs.json",
                     status_file=tmp_path / "status.json", run_id="fresh-run", run_issue_cycle=True,
                     run_pr_cycle=True, issues_data={"issues": []})
    result = run_discover_phase(ctx)
    assert [finding.finding_id for finding in result[2]] == expected
    assert ctx.issues_data == {"issues": []}
    if not expected:
        assert result[5]
        if status == "ran":
            assert "selected finding was not uniquely rediscovered in this run" in result[5]
