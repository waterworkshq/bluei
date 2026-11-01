"""Proven Markdown aliases share routing guards without rewriting history."""
import copy
import json
from types import SimpleNamespace

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.discover import run_discover_phase
from bluei.engine.models import Finding, now_iso, stable_finding_id


REPO = "/isolated/application"


def _finding(rule):
    return Finding(finding_id=stable_finding_id(REPO, "README.md", 3, rule, "Trailing spaces"),
                   repo=REPO, path="README.md", line=3, rule=rule, snippet="Trailing spaces",
                   confidence=0.9, quick_win=False, safe_to_autofix=False)


def _run(tmp_path, monkeypatch, state, issues, phase="issue-cycle"):
    from bluei.engine import orchestrator
    legacy, current = _finding("MD009"), _finding("md-MD009")
    history = tmp_path / "findings.jsonl"
    history.write_text("".join(json.dumps(f.to_dict()) + "\n" for f in (legacy, current)))
    before = history.read_bytes()
    finding = _finding("md-MD009")

    def discover(repo, outcomes=None, **kwargs):
        outcomes["plugin:plugin-markdown"] = {"status": "ran", "count": 1}
        return [finding]

    monkeypatch.setattr(orchestrator, "discover_findings", discover)
    args = SimpleNamespace(run_phase=phase, open_issues_cap=20,
                           finding_cooldown_seconds=3600, live_github_actions=False,
                           issue_confidence_threshold=0.7)
    ctx = RunContext(args=args, repo_path=tmp_path, log_file=tmp_path / "run.log",
                     findings_file=history, docs_index_file=tmp_path / "docs.json",
                     status_file=tmp_path / "status.json", state=state, issues_data=issues,
                     run_id="fresh", run_issue_cycle=True, run_pr_cycle=False)
    result = run_discover_phase(ctx)
    assert history.read_bytes() == before
    if any("manual resolution" in reason for reason in result[5]):
        current = json.loads((tmp_path / "discovery-current.json").read_text())
        assert current["detector_outcomes"]["markdown-identity"]["status"] == "failed"
    return result


@pytest.mark.parametrize("activity_rule", ["MD009", "md-MD009"])
def test_either_alias_cooldown_suppresses_rediscovery(tmp_path, monkeypatch, activity_rule):
    activity_id = _finding(activity_rule).finding_id
    state = {"finding_activity": {activity_id: {"last_action_at": now_iso(), "failure_count": 2}}}
    before = copy.deepcopy(state)
    result = _run(tmp_path, monkeypatch, state, {"issues": []})
    assert not result[2] and len(result[3]) == 1
    assert state == before


@pytest.mark.parametrize("issue_rule", ["MD009", "md-MD009"])
def test_reuses_existing_issue_id_without_rewriting_pr_link(tmp_path, monkeypatch, issue_rule):
    linked_id = _finding(issue_rule).finding_id
    issue = {"issue_id": "existing", "finding_id": linked_id, "status": "open",
             "github": {"issue_number": 11, "pr_number": 12, "pr_url": "https://example.test/12"}}
    issues = {"issues": [issue]}
    before = copy.deepcopy(issues)
    result = _run(tmp_path, monkeypatch, {}, issues)
    assert [f.finding_id for f in result[2]] == [linked_id]
    assert issues == before


@pytest.mark.parametrize("recent_activity", [False, True])
def test_two_linked_aliases_require_manual_resolution_without_mutation(tmp_path, monkeypatch, recent_activity):
    issues = {"issues": [{"issue_id": rule, "finding_id": _finding(rule).finding_id,
                           "status": "open", "github": {"pr_number": number}}
                          for number, rule in enumerate(("MD009", "md-MD009"), 1)]}
    before = copy.deepcopy(issues)
    state = {"finding_activity": {_finding("MD009").finding_id: {"last_action_at": now_iso()}}} if recent_activity else {}
    result = _run(tmp_path, monkeypatch, state, issues)
    assert not result[2] and len(result[3]) == 1
    assert any("alias" in reason and "manual" in reason for reason in result[5])
    assert issues == before


def test_resolved_alias_history_does_not_block_current_discovery(tmp_path, monkeypatch):
    ids = [_finding(rule).finding_id for rule in ("MD009", "md-MD009")]
    issues = {"issues": [{"finding_id": fid, "status": "resolved_merged", "github": {"pr_number": n}}
                          for n, fid in enumerate(ids, 1)]}
    state = {"created": [{"type": "pr", "linked_finding_ids": [fid], "github_pr_number": n}
                          for n, fid in enumerate(ids, 1)]}
    before = copy.deepcopy((issues, state))
    result = _run(tmp_path, monkeypatch, state, issues)
    assert len(result[2]) == 1 and not result[5]
    assert issues == before[0] and state["created"] == before[1]["created"]


def test_resolved_verified_with_pr_is_not_treated_as_closed_pr(tmp_path, monkeypatch):
    canonical = _finding("md-MD009").finding_id
    issues = {"issues": [{"finding_id": canonical, "status": "resolved_verified", "github": {"pr_number": 11}}]}
    before = copy.deepcopy(issues)
    result = _run(tmp_path, monkeypatch, {}, issues)
    assert [finding.finding_id for finding in result[2]] == [canonical]
    assert issues == before


def test_recurrent_markdown_issue_reopens_without_rewriting_links(tmp_path):
    from bluei.engine.commands.issue_cycle import run_issue_creation_phase
    finding = _finding("md-MD009")
    issue = {"issue_id": "old", "finding_id": finding.finding_id, "status": "resolved_verified", "github": {"issue_number": 5}}
    ctx = RunContext(args=SimpleNamespace(dry_run=False, max_issues_per_run=1, open_issues_cap=20,
                                         issue_confidence_threshold=0.7, live_github_actions=False), eligible_findings=[finding],
                     issues_data={"issues": [issue]}, log_file=tmp_path / "run.log")
    result = run_issue_creation_phase(ctx)
    assert result[1] == 1
    assert issue["status"] == "open" and issue["github"] == {"issue_number": 5}
    assert len(ctx.issues_data["issues"]) == 1


@pytest.mark.parametrize("limit,cap,confidence", [(0, 20, 0.9), (1, 0, 0.9), (1, 20, 0.1)])
def test_recurrence_respects_budget_cap_and_confidence(tmp_path, limit, cap, confidence):
    from bluei.engine.commands.issue_cycle import run_issue_creation_phase
    finding = _finding("md-MD009")
    finding.confidence = confidence
    issue = {"issue_id": "old", "finding_id": finding.finding_id, "status": "resolved_verified"}
    ctx = RunContext(args=SimpleNamespace(dry_run=False, max_issues_per_run=limit, open_issues_cap=cap,
                                         issue_confidence_threshold=0.7, live_github_actions=False),
                     eligible_findings=[finding], issues_data={"issues": [issue]}, log_file=tmp_path / "run.log")
    assert run_issue_creation_phase(ctx)[1] == 0
    assert issue["status"] == "resolved_verified"


def test_merged_recurrence_preserves_previous_pr_in_history(tmp_path):
    from bluei.engine.commands.issue_cycle import run_issue_creation_phase
    finding = _finding("md-MD009")
    issue = {"issue_id": "old", "finding_id": finding.finding_id, "status": "resolved_merged", "github": {"pr_number": 7, "issue_number": 5}}
    ctx = RunContext(args=SimpleNamespace(dry_run=False, max_issues_per_run=1, open_issues_cap=20,
                                         issue_confidence_threshold=0.7, live_github_actions=False),
                     eligible_findings=[finding], issues_data={"issues": [issue]}, log_file=tmp_path / "run.log")
    assert run_issue_creation_phase(ctx)[1] == 1
    assert issue["status"] == "open" and issue["github"] == {"issue_number": 5}
    assert any(item.get("github", {}).get("pr_number") == 7 for item in issue["history"])


@pytest.mark.parametrize("result", [0, 1, "timeout"])
def test_live_recurrence_reopens_same_github_issue_before_local_status(tmp_path, result):
    from bluei.engine.commands.issue_cycle import run_issue_creation_phase
    from unittest.mock import patch
    finding = _finding("md-MD009")
    issue = {"issue_id": "old", "finding_id": finding.finding_id, "status": "resolved_verified", "github": {"issue_number": 5}}
    ctx = RunContext(args=SimpleNamespace(dry_run=False, max_issues_per_run=1, open_issues_cap=20,
                                         issue_confidence_threshold=0.7, live_github_actions=True, repo_path=str(tmp_path)),
                     gh_repo_slug="acme/app", eligible_findings=[finding], issues_data={"issues": [issue]},
                     log_file=tmp_path / "run.log")
    def reopen(*args, **kwargs):
        assert issue["status"] == "resolved_verified"
        if result == "timeout":
            import subprocess
            raise subprocess.TimeoutExpired("gh", 30)
        return result, ""
    with patch("bluei.engine.utils.run_capture", side_effect=reopen) as command:
        assert run_issue_creation_phase(ctx)[1] == (1 if result == 0 else 0)
    command.assert_called_once_with(["gh", "issue", "reopen", "5", "--repo", "acme/app"], cwd=tmp_path)
    assert issue["status"] == ("open" if result == 0 else "resolved_verified")


def test_merged_pr_is_not_reused_for_recurrent_finding(tmp_path):
    from bluei.engine.gh import create_or_update_github_pr
    from unittest.mock import patch
    with patch("bluei.engine.gh.find_existing_github_pr", return_value={"number": 7, "state": "MERGED"}):
        result = create_or_update_github_pr("acme/app", _finding("md-MD009"), "new-branch", 5,
                                          True, tmp_path / "run.log", tmp_path)
    assert result["created"] is True


@pytest.mark.parametrize("metadata", [[], "", "invalid", 0])
def test_malformed_metadata_never_reopens_or_enters_pr_queue(tmp_path, metadata):
    from bluei.engine.commands.issue_cycle import run_issue_creation_phase
    from bluei.engine.commands.pr_cycle import _select_candidates
    finding = _finding("md-MD009")
    issue = {"issue_id": "old", "finding_id": finding.finding_id, "status": "resolved_verified",
             "rule": finding.rule, "path": finding.path, "line": finding.line, "snippet": finding.snippet,
             "confidence": 0.9, "safe_to_autofix": False, "github": metadata}
    args = SimpleNamespace(dry_run=False, max_issues_per_run=1, open_issues_cap=20,
                           issue_confidence_threshold=0.7, live_github_actions=False)
    ctx = RunContext(args=args, eligible_findings=[finding], issues_data={"issues": [issue]},
                     log_file=tmp_path / "run.log")
    assert run_issue_creation_phase(ctx)[1] == 0
    assert issue["status"] == "resolved_verified" and issue["github"] == metadata
    assert _select_candidates(repo_path=tmp_path, findings_file=tmp_path / "findings.jsonl",
                              log_file=tmp_path / "run.log", gh_repo_slug="acme/app", args=args,
                              issues_data={"issues": [issue]}) == ([], 0)


def test_verify_only_ignores_routing_conflict(tmp_path, monkeypatch):
    issues = {"issues": [{"finding_id": _finding(rule).finding_id, "status": "open"}
                          for rule in ("MD009", "md-MD009")]}
    before = copy.deepcopy(issues)
    result = _run(tmp_path, monkeypatch, {}, issues, phase="verify-only")
    assert len(result[0]) == 1 and not result[5]
    assert issues == before


@pytest.mark.parametrize("old_rule,current_rule", [("MD009", "md-MD009"), ("md-MD009", "MD009")])
def test_rule_alias_does_not_prove_closure(tmp_path, old_rule, current_rule):
    from bluei.engine.validation import verify_fix_closed
    old, current = _finding(old_rule), _finding(current_rule)
    current.line += 1
    assert not verify_fix_closed(tmp_path, old, tmp_path / "run.log", discover_fn=lambda *a, **kw: [current])


def test_verification_only_preserves_firing_legacy_issue(tmp_path):
    from bluei.engine.commands.verify_only import run_verify_only
    from unittest.mock import patch
    issue = {"issue_id": "legacy", "rule": "MD009", "path": "README.md", "status": "open"}
    ctx = RunContext(args=SimpleNamespace(), findings=[_finding("md-MD009")],
                     issues_data={"issues": [issue]}, state_file=tmp_path / "state.json",
                     issues_file=tmp_path / "issues.json", status_file=tmp_path / "status.json",
                     log_file=tmp_path / "run.log", findings_file=tmp_path / "findings.jsonl",
                     detector_outcomes={"plugin:plugin-markdown": {"status": "ran", "count": 1}})
    with patch("bluei.engine.commands.verify_only.update_status_artifact"):
        assert run_verify_only(ctx) == 0
    assert issue["status"] == "fix_failed_verification"


def test_preserves_existing_pr_record_without_issue(tmp_path, monkeypatch):
    canonical = _finding("md-MD009").finding_id
    state = {"created": [{"type": "pr", "linked_finding_ids": [canonical], "github_pr_number": 12}]}
    before = copy.deepcopy(state)
    result = _run(tmp_path, monkeypatch, state, {"issues": []})
    assert [f.finding_id for f in result[2]] == [canonical]
    assert state["created"] == before["created"]
