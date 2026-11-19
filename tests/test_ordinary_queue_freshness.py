"""Ordinary-queue freshness gate on ``_select_candidates`` (Stage 2).

Regression conditions for the fresh-evidence gate: when ``fresh_findings``
is supplied, an issue joins the ordinary repair queue only on exactly one
unambiguous fresh match whose responsible detector ran clean, and only when
exactly one active issue claims that alias group. The gate is exercised
directly with real identity-hashed findings files (``stable_finding_id``
records) — nothing about the gate itself is mocked.

Pinned semantics under test:

- No ``fresh_findings`` -> legacy selection is preserved byte-for-byte.
- Stale/absent fresh evidence -> issue skipped as unqualified, record left
  untouched (stays open, open-PR link retained; no new status is written).
- Ambiguous alias group (multiple fresh matches) or duplicate active issues
  in one group -> refused.
- Producer outcome failed/unavailable, outcomes missing, or responsible
  detector did not run -> refused.
- Saved issue safety bits are never trusted nor rewritten: the queued
  Finding is ``replace(fresh, finding_id=saved_id)`` so fresh metadata
  controls routing while queue-id history survives. A fresh unsafe E501
  (not LLM-fixable) therefore never reaches recipe/model lanes, while a
  fresh B904 case-alias stays model-fixable.
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bluei.engine.commands.pr_cycle import (
    _model_lane_eligible,
    _select_candidates,
)
from bluei.engine.models import stable_finding_id

REPO = "test-repo"
PATH = "src/main.py"
B904_SNIPPET = "raise ValueError(msg)"
E501_SNIPPET = "x = 1  # line that runs far past the configured line-length limit"
RAN = {"python-linter-ruff": {"status": "ran", "count": 1}}


def _fid(rule: str, line: int = 4, snippet: str = B904_SNIPPET) -> str:
    return stable_finding_id(REPO, PATH, line, rule, snippet)


def _write_findings(findings_file: Path, rules) -> None:
    """Persist identity-valid findings records: finding_id must equal the
    hash recomputed from the record's own fields or the identity loader
    silently drops the record (truncated-snippet contract)."""
    lines = []
    for rule in rules:
        record = {
            "finding_id": _fid(rule),
            "repo": REPO,
            "path": PATH,
            "line": 4,
            "rule": rule,
            "snippet": B904_SNIPPET,
            "confidence": 0.9,
        }
        lines.append(json.dumps(record))
    findings_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _issue(rule: str, finding_id: str, **overrides) -> dict:
    base = {
        "issue_id": "i1",
        "finding_id": finding_id,
        "status": "open",
        "rule": rule,
        "repo": REPO,
        "path": PATH,
        "line": 4,
        "snippet": B904_SNIPPET,
        "confidence": 0.9,
        "quick_win": True,
        "safe_to_autofix": True,
    }
    base.update(overrides)
    return base


def _args(**overrides) -> SimpleNamespace:
    defaults = dict(
        live_github_actions=False,
        issue_confidence_threshold=0.5,
        max_fix_attempts_per_issue=3,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _select(tmp_path: Path, issues, *, fresh=None, outcomes=None,
            args=None, repo_path=None, call: int = 0):
    log_file = tmp_path / f"run-{call}.log"
    # Success paths never append to the log; touch so read_text always works.
    log_file.touch()
    result, escalated = _select_candidates(
        repo_path=repo_path or (tmp_path / "repo"),
        findings_file=tmp_path / "findings.jsonl",
        log_file=log_file,
        gh_repo_slug="acme/repo",
        args=args or _args(),
        issues_data={"issues": issues},
        fresh_findings=fresh,
        detector_outcomes=outcomes,
    )
    return result, escalated, log_file.read_text()


def test_without_fresh_args_preserves_legacy_selection(tmp_path, make_finding):
    saved_id = _fid("ruff-c408")
    issue = _issue("ruff-c408", saved_id)
    findings_file = tmp_path / "findings.jsonl"
    _write_findings(findings_file, ["ruff-c408"])

    candidates, escalated, log = _select(tmp_path, [issue])

    assert escalated == 0
    assert "unqualified" not in log
    assert len(candidates) == 1
    assert candidates[0][0] is issue
    # Legacy path: the persisted record still drives routing metadata.
    assert candidates[0][1].finding_id == saved_id
    assert candidates[0][1].safe_to_autofix is True


def test_stale_finding_is_skipped_unqualified_and_open_pr_link_retained(
    tmp_path, make_finding, git_repo, git_commit_all
):
    (git_repo / PATH).parent.mkdir(parents=True)
    (git_repo / PATH).write_text("try:\n    run()\nexcept ValueError:\n    raise\n")
    git_commit_all(git_repo, "track source")
    saved_id = _fid("ruff-B904")
    issue = _issue(
        "ruff-B904",
        saved_id,
        github={"pr_number": 7, "pr_url": "https://github.com/acme/repo/pull/7"},
    )
    before = copy.deepcopy(issue)
    _write_findings(tmp_path / "findings.jsonl", ["ruff-B904"])
    # Current run only rediscovered an unrelated E501 elsewhere in the repo.
    stale_world = make_finding(
        finding_id=_fid("ruff-e501", line=30, snippet=E501_SNIPPET),
        rule="ruff-e501",
        line=30,
        snippet=E501_SNIPPET,
        safe_to_autofix=False,
    )

    with patch(
        "bluei.engine.commands.pr_cycle._reconcile_issue_pr_link",
        return_value=False,
    ):
        candidates, escalated, log = _select(
            tmp_path, [issue], fresh=[stale_world], outcomes=RAN,
            args=_args(live_github_actions=True), repo_path=git_repo,
        )

    assert candidates == []
    assert escalated == 0
    # No new status: the stale issue stays open, PR linkage retained.
    assert issue == before
    assert "unqualified queue" in log
    assert saved_id in log


def test_ambiguous_alias_group_with_multiple_fresh_matches_refused(tmp_path, make_finding):
    # Case variants of the code letter are the only valid alias spellings
    # ("ruff-" prefix must stay lowercase), so the group holds two ids.
    saved_id = _fid("ruff-B904")
    issue = _issue("ruff-B904", saved_id)
    before = copy.deepcopy(issue)
    _write_findings(tmp_path / "findings.jsonl", ["ruff-B904", "ruff-b904"])
    fresh_a = make_finding(finding_id=_fid("ruff-B904"), rule="ruff-B904",
                           safe_to_autofix=False)
    fresh_b = make_finding(finding_id=_fid("ruff-b904"), rule="ruff-b904",
                           safe_to_autofix=False)

    candidates, _, log = _select(
        tmp_path, [issue], fresh=[fresh_a, fresh_b], outcomes=RAN
    )

    assert candidates == []
    assert issue == before
    assert "unqualified queue" in log


def test_duplicate_active_issues_in_alias_group_refuse_both(tmp_path, make_finding):
    upper_id = _fid("ruff-B904")
    lower_id = _fid("ruff-b904")
    issue_a = _issue("ruff-B904", upper_id, issue_id="i1")
    issue_b = _issue("ruff-b904", lower_id, issue_id="i2")
    before = [copy.deepcopy(issue_a), copy.deepcopy(issue_b)]
    _write_findings(tmp_path / "findings.jsonl", ["ruff-B904", "ruff-b904"])
    fresh = make_finding(finding_id=lower_id, rule="ruff-b904",
                         safe_to_autofix=False)

    candidates, _, log = _select(
        tmp_path, [issue_a, issue_b], fresh=[fresh], outcomes=RAN
    )

    assert candidates == []
    assert [issue_a, issue_b] == before
    assert "unqualified queue" in log


def test_incomplete_producer_evidence_refuses_fresh_match(tmp_path, make_finding):
    saved_id = _fid("ruff-b904")
    fresh = make_finding(finding_id=saved_id, rule="ruff-b904",
                         safe_to_autofix=False)
    _write_findings(tmp_path / "findings.jsonl", ["ruff-b904"])

    failed = {"python-linter-ruff": {"status": "failed", "count": 0}}
    unrelated = {"plugin:plugin-typescript": {"status": "ran", "count": 0}}

    for call, outcomes in enumerate((failed, unrelated, None)):
        issue = _issue("ruff-b904", saved_id)
        before = copy.deepcopy(issue)
        candidates, _, log = _select(
            tmp_path, [issue], fresh=[fresh], outcomes=outcomes, call=call
        )
        assert candidates == [], outcomes
        assert issue == before, outcomes
        assert "unqualified queue" in log, outcomes


def test_fresh_safety_bits_override_saved_record_e501_not_autoqueued(tmp_path, make_finding):
    e501_id = stable_finding_id(REPO, PATH, 4, "ruff-e501", E501_SNIPPET)
    # Historical record claims the finding was recipe-safe to autofix.
    issue = _issue("ruff-e501", e501_id, snippet=E501_SNIPPET,
                   safe_to_autofix=True)
    findings_file = tmp_path / "findings.jsonl"
    findings_file.write_text(
        json.dumps({
            "finding_id": e501_id, "repo": REPO, "path": PATH, "line": 4,
            "rule": "ruff-e501", "snippet": E501_SNIPPET, "confidence": 0.9,
        }) + "\n",
        encoding="utf-8",
    )
    # Current run says the same E501 is NOT safe to autofix.
    fresh = make_finding(finding_id=e501_id, rule="ruff-e501", line=4,
                         snippet=E501_SNIPPET, safe_to_autofix=False)

    candidates, _, log = _select(tmp_path, [issue], fresh=[fresh], outcomes=RAN)

    # Fresh bit controls routing: E501 is neither recipe-safe nor
    # LLM-fixable, so no automatic recipe/model lane may queue it.
    assert candidates == []
    assert all(not f.safe_to_autofix for _, f in candidates)
    assert "not autofixable" in log
    assert issue["status"] == "needs-human-not-fixable"
    # Persisted safety bits are never rewritten by Stage 2.
    assert issue["safe_to_autofix"] is True


def test_b904_case_alias_fresh_match_preserves_issue_id_and_model_lane(tmp_path, make_finding):
    saved_id = _fid("ruff-B904")  # history recorded with the uppercase variant
    issue = _issue("ruff-B904", saved_id)
    _write_findings(tmp_path / "findings.jsonl", ["ruff-B904", "ruff-b904"])
    fresh = make_finding(finding_id=_fid("ruff-b904"), rule="ruff-b904",
                         safe_to_autofix=False)

    candidates, escalated, log = _select(
        tmp_path, [issue], fresh=[fresh], outcomes=RAN
    )

    assert escalated == 0
    assert "unqualified" not in log
    assert len(candidates) == 1
    queued_issue, queued_finding = candidates[0]
    assert queued_issue is issue
    # Queue-id history survives; fresh metadata controls the route.
    assert queued_finding.finding_id == saved_id
    assert queued_finding.rule == "ruff-b904"
    assert queued_finding.safe_to_autofix is False
    # Explicit deterministic routing: unsafe B904 still qualifies for the
    # model lane via its LLM-fixable rule.
    assert _model_lane_eligible(queued_finding) is True
