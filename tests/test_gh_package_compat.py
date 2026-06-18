"""Package-compatibility scaffolding tests for ``bluei.engine.gh``.

These tests pin the public contract of ``bluei.engine.gh`` across the
god-module decomposition (see ``docs/plans/god-module-decomp/gh.md``):

* ``test_public_symbols_reexported`` -- all 26 public symbols stay importable
  from ``bluei.engine.gh`` after the package conversion.
* ``test_patch_surface_still_works`` -- the test-patch surface
  (``bluei.engine.gh.{gh_json, run_capture, time}``) continues to affect the
  real call sites.  This is the load-bearing regression guard for Risk #1
  (section 4.1 of the plan): if it regresses, tests elsewhere would silently
  invoke real ``gh`` subprocesses instead of mocks.
* ``test_workspace_root_resolution`` -- ``repo_is_sandbox`` resolves the
  workspace root correctly.  Guards the ``Path(__file__).parents[N]`` hazard
  (Risk #2, section 4.2) once the code moves deeper in the package tree.

These are characterization tests: they pass against the current monolith and
MUST keep passing after every extraction step.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def test_public_symbols_reexported():
    """All 26 public symbols must be importable from bluei.engine.gh."""
    from bluei.engine import gh

    expected = [
        "gh_json",
        "finding_dedupe_marker",
        "run_capture",
        "time",
        "get_origin_url",
        "parse_github_repo",
        "parse_issue_number_from_url",
        "parse_pr_number_from_url",
        "repo_is_sandbox",
        "create_or_update_github_issue",
        "find_existing_github_issue",
        "finding_from_issue_record",
        "gh_issue_close",
        "gh_issue_comment",
        "create_or_update_github_pr",
        "find_batch_pr_by_rule",
        "find_existing_github_pr",
        "gh_pr_comment",
        "merge_failure_requires_pr_fix",
        "merge_pr",
        "evaluate_pr_check_health",
        "evaluate_pr_mergeability",
        "evaluate_pr_reviews",
        "fetch_open_prs_for_merge",
        "evaluate_pr_regression",
        "fetch_github_live_counts",
    ]
    for name in expected:
        assert hasattr(gh, name), f"bluei.engine.gh.{name} missing after extraction"


def test_patch_surface_still_works(tmp_path: Path):
    """Patching bluei.engine.gh.{gh_json,run_capture,time} must affect call sites.

    This is the load-bearing regression guard.  If it fails after an
    extraction, the patch surface broke (Risk #1, section 4.1) and tests
    elsewhere would silently invoke real subprocesses instead of mocks.
    """
    from bluei.engine import gh

    # (a) gh_json patch surface -- find_existing_github_issue calls gh_json by
    #     bare name; the patch must reach that call site.
    with patch.object(gh, "gh_json", return_value=[]) as mock_gh_json:
        gh.find_existing_github_issue("owner/repo", "finding-123", cwd=tmp_path)
        assert mock_gh_json.called, (
            "Patch on bluei.engine.gh.gh_json did not affect find_existing_github_issue"
        )

    # (b) run_capture patch surface -- gh_json's internal run_capture(...) call
    #     must resolve to the patched value.  This is the canary for the Step-1
    #     extraction: once gh_json moves into _core.py, a naive
    #     ``from bluei.engine.utils import run_capture`` inside _core would make
    #     this patch silently miss the call site.  Asserting the return value
    #     (not just ``mock.called``) proves the patched value flowed through.
    with patch.object(
        gh, "run_capture", return_value=(0, '{"key": "value"}')
    ) as mock_rc:
        result = gh.gh_json(["gh", "test"], cwd=tmp_path)
        assert mock_rc.called, (
            "Patch on bluei.engine.gh.run_capture did not affect gh_json"
        )
        assert result == {"key": "value"}, (
            f"gh_json did not see the patched run_capture value: got {result!r}"
        )

    # (c) time.sleep patch surface -- gh_json's exponential backoff calls
    #     time.sleep; patching bluei.engine.gh.time.sleep must reach that call
    #     (``time`` is a shared module object, so the attribute patch
    #     propagates to every module that references the time module).
    with (
        patch.object(gh, "run_capture", return_value=(1, "")),
        patch.object(gh.time, "sleep") as mock_sleep,
    ):
        gh.gh_json(["gh", "test"], cwd=tmp_path)
        assert mock_sleep.called, (
            "Patch on bluei.engine.gh.time.sleep did not affect gh_json backoff"
        )


def test_workspace_root_resolution():
    """repo_is_sandbox must resolve workspace root correctly regardless of depth.

    Guards the Path(__file__).parents[N] hazard (Risk #2, section 4.2): once
    _load_self_merge_repos_from_global_config moves into the package, the
    parents index must be adjusted or sandbox/self-merge policy silently
    mis-classifies repos.
    """
    from bluei.engine import gh

    # Should not raise and must return a bool for an arbitrary slug.
    result = gh.repo_is_sandbox("owner/some-repo")
    assert isinstance(result, bool)


# --- Step 2: repo identity helpers extraction ---


def test_parse_github_repo_ssh_url():
    """Step 2 characterization: SSH ``git@github.com:owner/repo`` format."""
    from bluei.engine.gh import parse_github_repo

    owner, repo = parse_github_repo("git@github.com:acme/widget.git")
    assert owner == "acme"
    assert repo == "widget"


def test_parse_github_repo_no_marker():
    """Step 2 characterization: non-GitHub URL returns ``('', '')``."""
    from bluei.engine.gh import parse_github_repo

    owner, repo = parse_github_repo("https://gitlab.com/acme/widget")
    assert owner == ""
    assert repo == ""


def test_get_origin_url_failure_returns_empty(tmp_path: Path):
    """Step 2 characterization: rc != 0 from run_capture yields empty string."""
    from unittest.mock import patch

    from bluei.engine import gh

    with patch.object(gh, "run_capture", return_value=(1, "")):
        result = gh.get_origin_url(tmp_path)
    assert result == ""


def test_repo_module_exports_all_four_parsers():
    """Step 2 extraction: repo module exposes the four moved helpers."""
    from bluei.engine.gh import repo

    for name in (
        "get_origin_url",
        "parse_github_repo",
        "parse_issue_number_from_url",
        "parse_pr_number_from_url",
    ):
        assert hasattr(repo, name), f"bluei.engine.gh.repo.{name} missing"


def test_bluei_engine_gh_get_origin_url_is_repo_get_origin_url():
    """Step 2 extraction: the facade re-export is the same callable.

    Asserts ``bluei.engine.gh.get_origin_url`` and
    ``bluei.engine.gh.repo.get_origin_url`` are literally the same object --
    if they diverged (e.g. the facade still held a stale local definition),
    patches on the facade would not reach the module-level call sites.
    """
    from bluei.engine import gh
    from bluei.engine.gh import repo

    assert gh.get_origin_url is repo.get_origin_url
    assert gh.parse_github_repo is repo.parse_github_repo
    assert gh.parse_issue_number_from_url is repo.parse_issue_number_from_url
    assert gh.parse_pr_number_from_url is repo.parse_pr_number_from_url


# --- Step 3: sandbox & self-merge policy extraction ---


def test_load_self_merge_repos_missing_config_file(tmp_path: Path, monkeypatch):
    """Step 3 characterization: missing config.yaml -> []."""
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(tmp_path))
    from bluei.engine.gh.sandbox import _load_self_merge_repos_from_global_config

    # No config.yaml in tmp_path
    assert _load_self_merge_repos_from_global_config() == []


def test_load_self_merge_repos_malformed_yaml(tmp_path: Path, monkeypatch):
    """Step 3 characterization: unparseable YAML -> []."""
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "config.yaml").write_text("not: valid: yaml: [[[", encoding="utf-8")
    from bluei.engine.gh.sandbox import _load_self_merge_repos_from_global_config

    assert _load_self_merge_repos_from_global_config() == []


def test_load_self_merge_repos_non_dict_payload(tmp_path: Path, monkeypatch):
    """Step 3 characterization: YAML that parses to a non-dict (e.g. list) -> []."""
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(tmp_path))
    (tmp_path / "config.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    from bluei.engine.gh.sandbox import _load_self_merge_repos_from_global_config

    assert _load_self_merge_repos_from_global_config() == []


def test_sandbox_repo_is_sandbox_callable():
    """Step 3 extraction: repo_is_sandbox lives on the sandbox module."""
    from bluei.engine.gh import sandbox

    assert callable(sandbox.repo_is_sandbox)
    assert sandbox.repo_is_sandbox("owner/qa-sandbox-repo") is True


def test_workspace_root_resolution_still_correct(tmp_path: Path, monkeypatch):
    """Step 3 extraction: the parents[2] -> parents[3] fix works.

    The workspace root resolved by _load_self_merge_repos_from_global_config
    MUST point at the real repo root (where config.yaml lives).  After the
    move into gh/sandbox.py the file is one level deeper, so parents[2]
    would resolve to bluei/ (which has no config.yaml) and the loader
    would silently return [].  We pin a config.yaml in a synthetic
    workspace and assert the loader reads it.

    Guards Risk #2 from the plan.
    """
    workspace = tmp_path
    (workspace / "config.yaml").write_text(
        "github:\n  self_merge_repos:\n    - acme/widget\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QA_AGENT_WORKSPACE", str(workspace))

    from bluei.engine.gh.sandbox import _load_self_merge_repos_from_global_config

    # If parents[3] is wrong, this returns [] and the assertion fails.
    result = _load_self_merge_repos_from_global_config()
    assert result == ["acme/widget"], (
        f"workspace root resolution broken -- got {result!r}; "
        "check Path(__file__).resolve().parents[N] index in sandbox.py"
    )

    # And repo_is_sandbox must classify accordingly.
    from bluei.engine.gh import sandbox

    monkeypatch.delenv("BLUEI_SELF_MERGE_REPOS", raising=False)
    assert sandbox.repo_is_sandbox("acme/widget") is True
    assert sandbox.repo_is_sandbox("fork/acme/widget") is True
    assert sandbox.repo_is_sandbox("acme/other") is False


# --- Step 4: issue lifecycle extraction ---


def test_issue_ops_module_exports_all_five_symbols():
    """Step 4 extraction: issue_ops module exposes the five moved functions."""
    from bluei.engine.gh import issue_ops

    for name in (
        "find_existing_github_issue",
        "gh_issue_comment",
        "gh_issue_close",
        "finding_from_issue_record",
        "create_or_update_github_issue",
    ):
        assert hasattr(issue_ops, name), f"bluei.engine.gh.issue_ops.{name} missing"


def test_bluei_engine_gh_issue_ops_same_object_as_module():
    """Step 4 extraction: facade re-exports are literally the same callables.

    Asserts each ``bluei.engine.gh.<name>`` is identical to
    ``bluei.engine.gh.issue_ops.<name>``.  If they diverged (e.g. the facade
    still held a stale local definition), patches on the facade would not
    reach the module-level call sites.
    """
    from bluei.engine import gh
    from bluei.engine.gh import issue_ops

    for name in (
        "find_existing_github_issue",
        "gh_issue_comment",
        "gh_issue_close",
        "finding_from_issue_record",
        "create_or_update_github_issue",
    ):
        facade_attr = getattr(gh, name)
        module_attr = getattr(issue_ops, name)
        assert facade_attr is module_attr, (
            f"bluei.engine.gh.{name} is not bluei.engine.gh.issue_ops.{name}"
        )


def test_finding_from_issue_record_missing_finding_id_returns_none():
    """Step 4 characterization: empty finding_id -> None (required-field guard)."""
    from bluei.engine.gh import finding_from_issue_record

    # finding_id empty, rule + path present
    result = finding_from_issue_record(
        {"finding_id": "", "path": "src/x.py", "rule": "E501"}
    )
    assert result is None


def test_finding_from_issue_record_rule_alias_normalization():
    """Step 4 characterization: legacy rule aliases are rewritten to xo-* names."""
    from bluei.engine.gh import finding_from_issue_record

    result = finding_from_issue_record(
        {
            "finding_id": "abc",
            "path": "src/x.py",
            "rule": "complexity",
            "line": "10",
            "confidence": "0.5",
            "repo": "acme/widget",
        }
    )
    assert result is not None
    assert result.rule == "xo-complexity"
    assert result.repo == "acme/widget"


def test_finding_from_issue_record_bad_line_and_empty_repo_defaults():
    """Step 4 characterization: unparseable line -> 0; empty repo -> qa-sandbox-repo."""
    from bluei.engine.gh import finding_from_issue_record

    result = finding_from_issue_record(
        {
            "finding_id": "abc",
            "path": "src/y.py",
            "rule": "E501",
            "line": "not-a-number",
            "confidence": "also-bad",
            "repo": "",
        }
    )
    assert result is not None
    assert result.line == 0
    assert result.confidence == 0.0
    assert result.repo == "qa-sandbox-repo"


def test_create_or_update_github_issue_dry_run_existing(tmp_path: Path):
    """Step 4 characterization: dry_run on an existing issue comments nothing,
    logs the dry-run line, and reports ``created=False`` with the issue number.

    Patches ``find_existing_github_issue`` on the facade to confirm the
    issue_ops call site resolves it through ``_facade`` (patch surface).
    """
    from unittest.mock import patch

    from bluei.engine.models import Finding

    from bluei.engine import gh

    finding = Finding(
        finding_id="fid-1",
        repo="acme/widget",
        path="src/a.py",
        line=12,
        rule="E501",
        snippet="x = 1",
        confidence=0.8,
        quick_win=False,
        safe_to_autofix=False,
    )
    log_file = tmp_path / "log.txt"
    existing = {"number": "42", "url": "https://github.com/acme/widget/issues/42"}

    # find_existing_github_issue is resolved through _facade inside issue_ops.
    with patch.object(
        gh, "find_existing_github_issue", return_value=existing
    ) as mock_find:
        result = gh.create_or_update_github_issue(
            "acme/widget", finding, dry_run=True, log_file=log_file, cwd=tmp_path
        )
    assert mock_find.called, (
        "find_existing_github_issue patch on facade did not reach issue_ops"
    )
    assert result == {
        "number": 42,
        "url": "https://github.com/acme/widget/issues/42",
        "created": False,
    }
    assert "would comment existing GitHub issue #42" in log_file.read_text()


# --- Step 5: PR lifecycle extraction ---


def test_pr_ops_module_exports_all_six_symbols():
    """Step 5 extraction: pr_ops module exposes the six moved functions."""
    from bluei.engine.gh import pr_ops

    for name in (
        "find_existing_github_pr",
        "find_batch_pr_by_rule",
        "gh_pr_comment",
        "merge_failure_requires_pr_fix",
        "merge_pr",
        "create_or_update_github_pr",
    ):
        assert hasattr(pr_ops, name), f"bluei.engine.gh.pr_ops.{name} missing"


def test_bluei_engine_gh_pr_ops_same_object_as_module():
    """Step 5 extraction: facade re-exports are literally the same callables.

    Asserts each ``bluei.engine.gh.<name>`` is identical to
    ``bluei.engine.gh.pr_ops.<name>`` (notably ``merge_pr`` and
    ``create_or_update_github_pr``, the two complex hubs).
    """
    from bluei.engine import gh
    from bluei.engine.gh import pr_ops

    for name in (
        "find_existing_github_pr",
        "find_batch_pr_by_rule",
        "gh_pr_comment",
        "merge_failure_requires_pr_fix",
        "merge_pr",
        "create_or_update_github_pr",
    ):
        facade_attr = getattr(gh, name)
        module_attr = getattr(pr_ops, name)
        assert facade_attr is module_attr, (
            f"bluei.engine.gh.{name} is not bluei.engine.gh.pr_ops.{name}"
        )


def test_merge_pr_dry_run_simulated(tmp_path: Path):
    """Step 5 characterization: dry_run returns success without invoking gh."""
    from unittest.mock import patch

    from bluei.engine import gh

    with patch.object(gh, "run_capture") as mock_rc:
        ok, reason = gh.merge_pr("acme/widget", 7, dry_run=True, cwd=tmp_path)
    assert not mock_rc.called, "dry_run merge must not call run_capture"
    assert ok is True
    assert reason == "dry-run-merge-simulated"


def test_merge_pr_safety_block_observe_mode(tmp_path: Path):
    """Step 5 characterization: observe-mode safety_config blocks the merge
    and short-circuits before ``run_capture`` is ever called.

    Guards that ``merge_pr`` still imports ``check_merge_allowed`` from its
    canonical location (``bluei.engine.safety_gates``) after the move into
    pr_ops.py.
    """
    from unittest.mock import patch

    from bluei.engine import gh

    safety = {"mode": "observe"}
    with patch.object(gh, "run_capture") as mock_rc:
        ok, reason = gh.merge_pr(
            "acme/widget", 7, dry_run=False, cwd=tmp_path, safety_config=safety
        )
    assert not mock_rc.called, "blocked merge must not call run_capture"
    assert ok is False
    assert reason.startswith("blocked-by-safety-mode:")


def test_create_or_update_github_pr_reuses_existing_via_facade(tmp_path: Path):
    """Step 5 patch-surface: ``find_existing_github_pr`` is resolved through
    ``_facade`` inside pr_ops -- patching it on the facade must reach the call
    site and yield the reuse-path result without invoking ``run_capture``.
    """
    from unittest.mock import patch

    from bluei.engine.models import Finding

    from bluei.engine import gh

    finding = Finding(
        finding_id="fid-pr-1",
        repo="acme/widget",
        path="src/b.py",
        line=3,
        rule="E501",
        snippet="y = 2",
        confidence=0.7,
        quick_win=False,
        safe_to_autofix=False,
    )
    log_file = tmp_path / "pr.log"
    existing = {"number": "99", "url": "https://github.com/acme/widget/pull/99"}

    with (
        patch.object(gh, "find_existing_github_pr", return_value=existing) as mock_find,
        patch.object(gh, "run_capture") as mock_rc,
    ):
        result = gh.create_or_update_github_pr(
            "acme/widget",
            finding,
            branch="fix/x",
            issue_number=None,
            dry_run=False,
            log_file=log_file,
            cwd=tmp_path,
        )
    assert mock_find.called, (
        "find_existing_github_pr patch on facade did not reach pr_ops"
    )
    assert not mock_rc.called, "reuse-existing path must not call run_capture"
    assert result == {
        "number": 99,
        "url": "https://github.com/acme/widget/pull/99",
        "created": False,
    }
    assert "reuse existing PR #99" in log_file.read_text()


# --- Step 6: merge-gate evaluators extraction ---


def test_merge_eval_module_exports_all_four_symbols():
    """Step 6 extraction: merge_eval module exposes the four moved functions."""
    from bluei.engine.gh import merge_eval

    for name in (
        "fetch_open_prs_for_merge",
        "evaluate_pr_check_health",
        "evaluate_pr_reviews",
        "evaluate_pr_mergeability",
    ):
        assert hasattr(merge_eval, name), f"bluei.engine.gh.merge_eval.{name} missing"


def test_bluei_engine_gh_merge_eval_same_object_as_module():
    """Step 6 extraction: facade re-exports are literally the same callables.

    Asserts each ``bluei.engine.gh.<name>`` is identical to
    ``bluei.engine.gh.merge_eval.<name>``.  If they diverged (e.g. the
    facade still held a stale local definition), patches on the facade
    would not reach the module-level call sites.
    """
    from bluei.engine import gh
    from bluei.engine.gh import merge_eval

    for name in (
        "fetch_open_prs_for_merge",
        "evaluate_pr_check_health",
        "evaluate_pr_reviews",
        "evaluate_pr_mergeability",
    ):
        facade_attr = getattr(gh, name)
        module_attr = getattr(merge_eval, name)
        assert facade_attr is module_attr, (
            f"bluei.engine.gh.{name} is not bluei.engine.gh.merge_eval.{name}"
        )


def test_fetch_open_prs_for_merge_resolves_gh_json_via_facade(tmp_path: Path):
    """Step 6 patch-surface: ``fetch_open_prs_for_merge`` calls ``gh_json``
    through ``_facade`` -- patching ``bluei.engine.gh.gh_json`` must reach the
    call site inside merge_eval.py and yield the sorted payload.
    """
    from unittest.mock import patch

    from bluei.engine import gh

    payload = [
        {"number": 2, "isDraft": False, "createdAt": "2024-01-02T00:00:00Z"},
        {"number": 1, "isDraft": False, "createdAt": "2024-01-01T00:00:00Z"},
        {"number": 3, "isDraft": True, "createdAt": "2024-01-01T00:00:00Z"},
    ]
    with patch.object(gh, "gh_json", return_value=payload) as mock_gh_json:
        result = gh.fetch_open_prs_for_merge("acme/widget", cwd=tmp_path)
    assert mock_gh_json.called, (
        "Patch on bluei.engine.gh.gh_json did not reach fetch_open_prs_for_merge"
    )
    # Non-drafts first, oldest first: PR 1 before PR 2; draft PR 3 last.
    assert [pr["number"] for pr in result] == [1, 2, 3]


def test_evaluate_pr_reviews_branch_protection_call_uses_facade(tmp_path: Path):
    """Step 6 patch-surface: the second ``gh_json`` call (branch protection
    lookup) inside ``evaluate_pr_reviews`` must also resolve through the
    facade.  When ``latestReviews`` is empty, the function falls through to
    the protection check; patching gh_json to return a no-protection dict
    yields the no-reviews-no-protection pass.
    """
    from unittest.mock import patch

    from bluei.engine import gh

    def fake_gh_json(cmd, cwd):
        # First call returns the PR view with empty reviews; second call
        # returns the branch-protection payload.
        if "pr" in cmd and "view" in cmd:
            return {"baseRefName": "main", "latestReviews": []}
        return {"required_status_checks": {}}  # no required_pull_request_reviews

    with patch.object(gh, "gh_json", side_effect=fake_gh_json) as mock_gh_json:
        result = gh.evaluate_pr_reviews("acme/widget", 7, cwd=tmp_path)
    assert mock_gh_json.call_count == 2, (
        "evaluate_pr_reviews should issue two gh_json calls (reviews + protection)"
    )
    assert result == {
        "eligible": True,
        "has_reviews": False,
        "reason": "no-reviews-no-protection-pass",
    }


# --- Step 7: PR regression audit extraction ---


def test_pr_regression_module_exports_evaluate_pr_regression():
    """Step 7 extraction: pr_regression module exposes the moved function."""
    from bluei.engine.gh import pr_regression

    assert hasattr(pr_regression, "evaluate_pr_regression"), (
        "bluei.engine.gh.pr_regression.evaluate_pr_regression missing"
    )


def test_bluei_engine_gh_pr_regression_same_object_as_module():
    """Step 7 extraction: facade re-export is literally the same callable.

    Asserts ``bluei.engine.gh.evaluate_pr_regression`` is identical to
    ``bluei.engine.gh.pr_regression.evaluate_pr_regression``.
    """
    from bluei.engine import gh
    from bluei.engine.gh import pr_regression

    assert gh.evaluate_pr_regression is pr_regression.evaluate_pr_regression, (
        "bluei.engine.gh.evaluate_pr_regression is not "
        "bluei.engine.gh.pr_regression.evaluate_pr_regression"
    )


def test_evaluate_pr_regression_pr_info_unavailable_uses_facade_gh_json(
    tmp_path: Path,
):
    """Step 7 patch-surface: when ``gh_json`` returns a non-dict, the function
    short-circuits via the facade-routed ``gh_json`` call.  Patching
    ``bluei.engine.gh.gh_json`` must reach the call site inside pr_regression.py.
    """
    from unittest.mock import patch

    from bluei.engine import gh

    with patch.object(gh, "gh_json", return_value=None) as mock_gh_json:
        result = gh.evaluate_pr_regression("acme/widget", 11, cwd=tmp_path)
    assert mock_gh_json.called, (
        "Patch on bluei.engine.gh.gh_json did not reach evaluate_pr_regression"
    )
    assert result["action"] == "safe-to-merge"
    assert result["pr_info"] is None
    assert result.get("error") == "pr-info-unavailable"


def test_evaluate_pr_regression_resolves_check_health_via_facade(tmp_path: Path):
    """Step 7 cross-module indirection: ``evaluate_pr_check_health`` moved
    into merge_eval.py in Step 6, so a bare-name call inside pr_regression
    would NameError.  Routing through ``_facade`` keeps both the call working
    AND the patch surface intact -- patching
    ``bluei.engine.gh.evaluate_pr_check_health`` must reach the call site.

    Uses a two-call side_effect on gh_json: first call returns a valid PR
    info dict (so the function proceeds past the early return), and we patch
    run_capture + check_health to avoid invoking gh subprocesses for the diff
    and the regression-engine imports stay lazy and unloaded on this path.
    """
    from unittest.mock import patch

    from bluei.engine import gh

    pr_info = {
        "number": 11,
        "headRefName": "feature-x",
        "baseRefName": "main",
        "title": "feat: x",
    }

    with (
        patch.object(gh, "gh_json", return_value=pr_info),
        patch.object(gh, "run_capture", return_value=(0, "")),
        patch.object(
            gh, "evaluate_pr_check_health", return_value={"eligible": True}
        ) as mock_health,
    ):
        result = gh.evaluate_pr_regression("acme/widget", 11, cwd=tmp_path)
    assert mock_health.called, (
        "Patch on bluei.engine.gh.evaluate_pr_check_health did not reach "
        "evaluate_pr_regression (cross-module _facade indirection broken)"
    )
    assert result["check_health"] == {"eligible": True}
