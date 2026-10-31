"""Selection must use fresh discovery and never route another finding."""
from types import SimpleNamespace

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.discover import run_discover_phase


@pytest.mark.parametrize("status,selected,present,expected", [
    ("ran", "f-selected", True, ["f-selected"]),
    ("failed", "f-selected", True, []),
    ("unavailable", "f-selected", True, []),
    ("skipped", "f-selected", True, []),
    ("ran", "absent", True, []),
    ("ran", "f-selected", False, []),
])
def test_selected_finding_requires_fresh_responsible_producer(
    tmp_path, monkeypatch, make_finding, status, selected, present, expected
):
    import bluei.engine.orchestrator as orchestrator

    actual = make_finding(finding_id="f-selected", rule="ruff-F821", path="app.py")
    other = make_finding(finding_id="f-other", rule="ruff-F821", path="other.py")

    def discover(repo, outcomes=None, **kwargs):
        assert kwargs["discovery_scope"] == "python"
        outcomes["plugin:plugin-python"] = {"status": status, "count": 2}
        return [actual, other] if present else [other]

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
