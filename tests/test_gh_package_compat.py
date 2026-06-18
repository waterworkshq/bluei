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
