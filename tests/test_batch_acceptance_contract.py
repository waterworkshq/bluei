"""Stage 3 batch acceptance contract tests.

process_batch must qualify a multi-finding batch through the shared
repair_acceptance helpers before any positive effect:

- missing acceptance inputs  -> Stage 1 containment refusal (unchanged)
- unusable worktree baseline -> abort BEFORE fix spend
- unqualified candidate      -> no commit/push/PR, no patch retention
- qualified + live           -> patch retained BEFORE commit/push/PR
- qualified + ordinary (no live) -> LOCAL acceptance: patch retained, no
  commit/push/PR, per-issue verified_fix_patch, issues stay open
- failed publication         -> truthful failure; retained patch is evidence,
  never a local success fallback
- split children             -> acceptance inputs + metrics merge

Every result is a BatchResult: 2-tuple unpackable AND carrying .metrics
with ACTUAL-call counters (attempts = dispatched members; deterministic/
model = real backend launches incl. failed ones; verified = accepted
members; prs_created = real PRs only).

All repair_acceptance helpers are monkeypatched fakes: these tests pin the
batch-side CONTRACT (ordering, kwargs, outcomes), not helper internals.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from bluei.engine import repair_acceptance
from bluei.engine import batch_pr
from bluei.engine.batch_execution import _apply_single_fix
from bluei.engine.batch_pr import parse_pr_count, process_batch
from bluei.engine.models import BatchGroup, FixResult

UNQUALIFIED_DETAIL = (
    "batch repair unqualified: missing configured scope/validation/closure acceptance"
)

BASELINE_CHECKS = {"lint": ["python", "-c", "pass"]}
REPO_BASELINE = {"lint": {"rc": 0, "ok": True}}


def _make_batch(make_finding, batch_id="batch-acc-001", findings=None):
    if findings is None:
        findings = [
            make_finding(finding_id="f001", path="src/a.py"),
            make_finding(finding_id="f002", path="src/b.py"),
        ]
    issues = [
        {"finding_id": f.finding_id, "id": f"ISS-{i}"} for i, f in enumerate(findings)
    ]
    return BatchGroup(
        batch_id=batch_id,
        rule_pattern="ruff-c408",
        group_by="rule",
        findings=findings,
        issues=issues,
    )


def _args(tmp_path, dry_run=True, live=False):
    return argparse.Namespace(
        dry_run=dry_run,
        live_github_actions=live,
        fix_engine="deterministic",
        worktree_root=str(tmp_path / "wtroot"),
        max_split_depth=3,
        batch_pr_split_on_failure=True,
        allow_unchanged_baseline_failures=False,
        max_files_changed=5,
        max_loc_diff=200,
        discovery_scope="repository",
        batch_state_file=str(tmp_path / "state" / "batch.yaml"),
        batch_dedup_hours=24,
        claude_cmd_template=None,
    )


def _git_init(worktree_path: Path):
    worktree_path.mkdir(parents=True, exist_ok=True)
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "t@t.co"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(cmd, cwd=str(worktree_path), capture_output=True, check=True)
    (worktree_path / "README").write_text("init\n")
    subprocess.run(["git", "add", "."], cwd=str(worktree_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=str(worktree_path), capture_output=True, check=True
    )


@pytest.fixture
def plumbing(monkeypatch):
    """Fake worktree plumbing; record every publication step."""
    calls = {"commit": [], "push": [], "worktrees": [], "removed": []}

    def _fake_create(repo_path, worktree_path, branch, log_file):
        _git_init(Path(worktree_path))
        calls["worktrees"].append(str(worktree_path))
        return True

    monkeypatch.setattr(batch_pr, "_create_worktree", _fake_create)
    monkeypatch.setattr(batch_pr, "hydrate_worktree", lambda *a, **k: None)
    monkeypatch.setattr(
        batch_pr, "remove_worktree", lambda **k: calls["removed"].append(k.get("branch"))
    )
    import bluei.engine.git_ops as git_ops

    def _fake_commit(path, message, log_file=None, dry_run=False, **k):
        calls["commit"].append(message)
        return "committed"

    def _fake_push(path, branch, log_file=None, dry_run=False, safety_config=None):
        calls["push"].append(branch)
        return True

    monkeypatch.setattr(git_ops, "git_commit_all", _fake_commit)
    monkeypatch.setattr(git_ops, "git_push_branch", _fake_push)
    return calls


@pytest.fixture
def fake_acceptance(monkeypatch, tmp_path):
    """Fakes for the three shared helpers; each stage recorded and steerable."""
    state = {
        "prepare": [],
        "validate": [],
        "retain": [],
        "prepare_result": ({}, {"passed": True, "qualified": True, "message": "ok"}),
        "validate_result": {
            "passed": True,
            "qualified": True,
            "message": "ok",
            "files_changed": 1,
            "loc_diff": 2,
            "candidate_signature": "sig-xyz",
        },
        "retain_result": tmp_path / "patches" / "batch-fix-x.patch",
    }

    def _prepare(**kwargs):
        state["prepare"].append(kwargs)
        return state["prepare_result"]

    def _validate(**kwargs):
        state["validate"].append(kwargs)
        return state["validate_result"]

    def _retain(**kwargs):
        state["retain"].append(kwargs)
        return state["retain_result"]

    monkeypatch.setattr(repair_acceptance, "prepare_baseline", _prepare)
    monkeypatch.setattr(repair_acceptance, "validate_candidate", _validate)
    monkeypatch.setattr(repair_acceptance, "retain_candidate_patch", _retain)
    return state


def _fake_apply(successes=2, deterministic=None):
    """apply_batch_fixes stand-in that drives the real counters dict."""

    def _apply(*, batch, counters=None, **kwargs):
        if counters is not None:
            counters["attempts"] += len(batch.findings)
            counters["deterministic"] += len(batch.findings) if deterministic is None else deterministic
            counters["model"] += 0
        return successes, len(batch.findings) - successes

    return _apply


class TestContainmentStays:
    def test_missing_inputs_refused_before_any_effect(
        self, tmp_path, make_finding, monkeypatch
    ):
        def _boom(*a, **k):
            raise AssertionError("no worktree may be created")

        monkeypatch.setattr(batch_pr, "_create_worktree", _boom)
        log_file = tmp_path / "run.log"
        result = process_batch(
            _make_batch(make_finding), tmp_path / "repo", _args(tmp_path), log_file
        )
        ok, detail = result  # 2-tuple unpack compat
        assert ok is False
        assert detail == UNQUALIFIED_DETAIL
        assert "batch-refused" in log_file.read_text()
        # Refusal still exposes structured (zeroed) metrics.
        assert result.metrics.attempts == 0
        assert result.metrics.prs_created == 0

    def test_baseline_checks_without_repo_results_refused(
        self, tmp_path, make_finding, fake_acceptance
    ):
        result = process_batch(
            _make_batch(make_finding),
            tmp_path / "repo",
            _args(tmp_path),
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
        )
        assert result.success is False
        assert result.detail == UNQUALIFIED_DETAIL
        assert fake_acceptance["prepare"] == []


class TestBaselineGate:
    def test_baseline_failure_aborts_before_fix_spend(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        fake_acceptance["prepare_result"] = (
            {},
            {"passed": False, "qualified": False, "message": "baseline broken"},
        )
        monkeypatch.setattr(
            batch_pr,
            "apply_batch_fixes",
            lambda **k: (_ for _ in ()).throw(AssertionError("no fix spend")),
        )
        batch = _make_batch(make_finding)
        result = process_batch(
            batch,
            tmp_path / "repo",
            _args(tmp_path),
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
        )
        assert result.success is False
        assert result.detail == "baseline-unqualified:baseline broken"
        assert batch.status == "failed"
        assert len(plumbing["worktrees"]) == 1
        assert fake_acceptance["validate"] == []
        assert fake_acceptance["retain"] == []
        assert plumbing["commit"] == []

    def test_prepare_receives_configured_inputs(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        process_batch(
            _make_batch(make_finding),
            tmp_path / "repo",
            _args(tmp_path),
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
            docs_index_file=tmp_path / "docs.json",
        )
        kwargs = fake_acceptance["prepare"][0]
        assert kwargs["checks"] is BASELINE_CHECKS
        assert kwargs["repo_baseline_results"] is REPO_BASELINE
        assert kwargs["source_repo"] == tmp_path / "repo"
        assert kwargs["allow_unchanged_baseline_failures"] is False


class TestCandidateGate:
    def test_unqualified_candidate_blocks_commit_and_patch(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        fake_acceptance["validate_result"] = {
            "passed": False,
            "qualified": False,
            "message": "detector-still-firing",
            "files_changed": 1,
            "loc_diff": 2,
        }
        monkeypatch.setattr(
            repair_acceptance,
            "retain_candidate_patch",
            lambda **k: (_ for _ in ()).throw(AssertionError("no patch on refusal")),
        )
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        batch = _make_batch(make_finding)
        result = process_batch(
            batch,
            tmp_path / "repo",
            _args(tmp_path),
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
        )
        assert result.success is False
        assert result.detail == "acceptance-unqualified:detector-still-firing"
        assert batch.status == "failed"
        assert plumbing["commit"] == []
        assert plumbing["push"] == []
        # Truthful counters: members were dispatched and attempted...
        assert result.metrics.attempts == 2
        # ...but nothing was accepted.
        assert result.metrics.verified_fixes == 0
        assert result.metrics.prs_created == 0
        assert "batch-patch-retained" not in (tmp_path / "run.log").read_text()

    def test_validate_receives_every_member_and_scope_inputs(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        args = _args(tmp_path)
        args.max_files_changed = 7
        args.max_loc_diff = 90
        process_batch(
            _make_batch(make_finding),
            tmp_path / "repo",
            args,
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
            docs_index_file=tmp_path / "docs.json",
        )
        kwargs = fake_acceptance["validate"][0]
        assert [f.finding_id for f in kwargs["findings"]] == ["f001", "f002"]
        assert kwargs["checks"] is BASELINE_CHECKS
        assert kwargs["max_files_changed"] == 7
        assert kwargs["max_loc_diff"] == 90
        assert kwargs["docs_index_file"] == tmp_path / "docs.json"
        assert kwargs["discovery_scope"] == "repository"
        # candidate_base is the real pre-fix HEAD captured in the worktree
        assert len(kwargs["candidate_base"]) == 40


class TestLocalMode:
    def test_ordinary_batch_never_publishes_and_accepts_locally(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        log_file = tmp_path / "run.log"
        batch = _make_batch(make_finding)
        result = process_batch(
            batch,
            tmp_path / "repo",
            _args(tmp_path, dry_run=False, live=False),
            log_file,
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
            artifact_dir=tmp_path / "artifacts",
        )
        # Local acceptance, not a PR.
        assert result.success is True
        assert result.detail == "batch-patch-verified"
        # NO commit/push/PR whatsoever.
        assert plumbing["commit"] == []
        assert plumbing["push"] == []
        # Retained patch is the artifact, into the caller-provided dir.
        assert fake_acceptance["retain"][0]["output_dir"] == tmp_path / "artifacts"
        assert f"batch-patch-retained: {fake_acceptance['retain_result']}" in log_file.read_text()
        # Every member issue carries the verified patch and stays open.
        for issue in batch.issues:
            assert issue["verified_fix_patch"] == str(fake_acceptance["retain_result"])
            assert issue["status"] == "open"
        assert batch.status == "patch-verified"
        # Metrics: dispatched attempts, all members verified, zero PRs.
        assert result.metrics.attempts == 2
        assert result.metrics.verified_fixes == 2
        assert result.metrics.prs_created == 0
        assert result.metrics.issue_patches == {
            "ISS-0": str(fake_acceptance["retain_result"]),
            "ISS-1": str(fake_acceptance["retain_result"]),
        }
        # The local detail never parses into a PR count.
        assert parse_pr_count(result.detail) == 0


class TestPublishMode:
    def test_dry_run_qualifies_retains_patch_then_simulates(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        log_file = tmp_path / "run.log"
        result = process_batch(
            _make_batch(make_finding),
            tmp_path / "repo",
            _args(tmp_path, dry_run=True),
            log_file,
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
        )
        assert result.success is True
        assert result.detail == "dry-run-pr-simulated"
        assert result.metrics.mode == "dry-run"
        # Patch retained BEFORE commit; commit then (dry) push ran.
        assert len(fake_acceptance["retain"]) == 1
        assert len(plumbing["commit"]) == 1
        retain_kwargs = fake_acceptance["retain"][0]
        assert retain_kwargs["prefix"] == "batch-fix"
        assert retain_kwargs["identity"] == "batch-acc-001"
        assert retain_kwargs["output_dir"] == tmp_path / "state" / "batch-patches"
        assert retain_kwargs["expected_signature"] == "sig-xyz"
        assert f"batch-patch-retained: {fake_acceptance['retain_result']}" in log_file.read_text()

    def test_publication_failure_is_truthful_not_local_fallback(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        import bluei.engine.gh as gh

        monkeypatch.setattr(gh, "get_origin_url", lambda p: "https://github.com/o/r.git")
        monkeypatch.setattr(gh, "parse_github_repo", lambda u: ("o", "r"))
        monkeypatch.setattr(gh, "find_batch_pr_by_rule", lambda *a, **k: None)
        monkeypatch.setattr(
            batch_pr,
            "create_batch_pr",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh down")),
        )
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        log_file = tmp_path / "run.log"
        batch = _make_batch(make_finding)
        result = process_batch(
            batch,
            tmp_path / "repo",
            _args(tmp_path, dry_run=False, live=True),
            log_file,
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
        )
        assert result.success is False
        assert result.detail == "pr-creation-failed"
        assert batch.status == "failed"
        # The qualified patch was retained as evidence, but the outcome is
        # still failure — no local success fallback, no PR counted.
        assert len(fake_acceptance["retain"]) == 1
        assert result.metrics.prs_created == 0
        assert result.metrics.verified_fixes == 0
        assert "batch-patch-retained" in log_file.read_text()

    def test_patch_retention_failure_blocks_publication(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        monkeypatch.setattr(
            repair_acceptance,
            "retain_candidate_patch",
            lambda **k: (_ for _ in ()).throw(ValueError("repair patch is empty")),
        )
        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _fake_apply())
        batch = _make_batch(make_finding)
        result = process_batch(
            batch,
            tmp_path / "repo",
            _args(tmp_path),
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
        )
        assert result.success is False
        assert result.detail == "patch-retention-failed"
        assert batch.status == "failed"
        assert plumbing["commit"] == []


class TestSplitChildrenInheritAcceptance:
    def test_split_child_receives_inputs_local_accepts_and_merges(
        self, tmp_path, make_finding, monkeypatch, plumbing, fake_acceptance
    ):
        child = _make_batch(
            make_finding,
            batch_id="batch-acc-child",
            findings=[make_finding(finding_id="f001", path="src/a.py")],
        )
        parent_findings = [
            make_finding(finding_id="f001", path="src/a.py"),
            make_finding(finding_id="f002", path="src/b.py"),
            make_finding(finding_id="f003", path="src/c.py"),
        ]

        def _apply(*, batch, counters=None, **kwargs):
            if batch.is_solo:  # the split child
                if counters is not None:
                    counters["attempts"] += 1
                    counters["deterministic"] += 1
                return 1, 0
            # Parent: 3 dispatched, 1 success / 2 failed -> >50% failure
            # rate -> split. should_split_batch reads fix_results, so the
            # fake must populate them exactly like the real executor.
            if counters is not None:
                counters["attempts"] += 3
                counters["deterministic"] += 3
            batch.fix_results = {
                "f001": FixResult(
                    finding_id="f001", status="success", fix_method="autofix"
                ),
                "f002": FixResult(
                    finding_id="f002", status="failed", error="x", fix_method="autofix"
                ),
                "f003": FixResult(
                    finding_id="f003", status="failed", error="x", fix_method="autofix"
                ),
            }
            return 1, 2

        monkeypatch.setattr(batch_pr, "apply_batch_fixes", _apply)
        monkeypatch.setattr(
            batch_pr, "handle_batch_failure", lambda batch, *a, **k: [child]
        )
        result = process_batch(
            _make_batch(make_finding, findings=parent_findings),
            tmp_path / "repo",
            _args(tmp_path, dry_run=False, live=False),
            tmp_path / "run.log",
            baseline_checks=BASELINE_CHECKS,
            repo_baseline_results=REPO_BASELINE,
            docs_index_file=tmp_path / "docs.json",
        )
        assert result.success is True
        # Child local-accepted (no PRs) -> zero PRs in the split aggregate.
        assert result.detail == "split-and-retried:0"
        assert result.metrics.attempts == 4  # 3 parent + 1 child
        assert result.metrics.verified_fixes == 1  # child member only
        assert result.metrics.prs_created == 0
        assert "ISS-0" in result.metrics.issue_patches
        # Child was validated through the same acceptance inputs.
        child_validations = [
            k for k in fake_acceptance["validate"]
            if len(k["findings"]) == 1 and k["findings"][0].finding_id == "f001"
        ]
        assert child_validations, "split child must be acceptance-validated"
        assert child_validations[0]["checks"] is BASELINE_CHECKS
        assert child_validations[0]["docs_index_file"] == tmp_path / "docs.json"
        # Parent and child each prepared a baseline from the repo results.
        assert len(fake_acceptance["prepare"]) == 2
        assert all(k["repo_baseline_results"] is REPO_BASELINE for k in fake_acceptance["prepare"])


class TestDeterministicEngineClamp:
    def _lifecycle_patches(self, monkeypatch, autofix=False, contextual=False):
        import bluei.engine.lifecycle as lifecycle
        import bluei.engine.context_fix as context_fix

        def _forbid_claude(*a, **k):
            raise AssertionError("model backend must not launch in deterministic engine")

        monkeypatch.setattr(lifecycle, "apply_claude_fix", _forbid_claude)
        monkeypatch.setattr(lifecycle, "apply_autofix", lambda *a, **k: autofix)
        monkeypatch.setattr(context_fix, "apply_contextual_fix", lambda **k: contextual)

    def test_deterministic_engine_never_falls_back_to_model(
        self, tmp_path, make_finding, monkeypatch, git_repo
    ):
        self._lifecycle_patches(monkeypatch, autofix=False, contextual=False)
        finding = make_finding(finding_id="f001", path="src/a.py")
        finding.safe_to_autofix = True
        counters = {"attempts": 0, "deterministic": 0, "model": 0}
        result = _apply_single_fix(
            finding=finding,
            worktree_path=git_repo,
            repo_path=git_repo,
            args=_args(tmp_path),
            log_file=tmp_path / "run.log",
            baseline_results=REPO_BASELINE,
            counters=counters,
        )
        assert result.status == "failed"
        assert result.error == "autofix-unavailable"
        # Two REAL deterministic launches (autofix + contextual), zero model.
        assert counters["deterministic"] == 2
        assert counters["model"] == 0

    def test_recipe_eligible_finding_attempted_in_deterministic_engine(
        self, tmp_path, make_finding, monkeypatch, git_repo
    ):
        # Ordinary-lane recipe eligibility: ruff-b904 is unsafe for the
        # automatic tool (safe_to_autofix=False) but has a deterministic
        # recipe — the batch must ATTEMPT it (real launches counted), not
        # skip, and never route to the model in the deterministic engine.
        self._lifecycle_patches(monkeypatch, autofix=False, contextual=False)
        finding = make_finding(finding_id="f001", path="src/a.py", rule="ruff-b904")
        finding.safe_to_autofix = False
        counters = {"attempts": 0, "deterministic": 0, "model": 0}
        result = _apply_single_fix(
            finding=finding,
            worktree_path=git_repo,
            repo_path=git_repo,
            args=_args(tmp_path),
            log_file=tmp_path / "run.log",
            baseline_results=REPO_BASELINE,
            counters=counters,
        )
        assert result.status == "failed"
        assert result.error == "autofix-unavailable"  # both recipe + contextual faked off
        assert counters["deterministic"] == 2
        assert counters["model"] == 0

    def test_no_recipe_unsafe_finding_still_skipped(
        self, tmp_path, make_finding, monkeypatch, git_repo
    ):
        # Skip semantics stay truthful for genuinely unsupported rules:
        # not safe, no recipe, deterministic engine -> no launch at all.
        self._lifecycle_patches(monkeypatch)
        finding = make_finding(
            finding_id="f001", path="src/a.py", rule="no-such-recipe-rule"
        )
        finding.safe_to_autofix = False
        counters = {"attempts": 0, "deterministic": 0, "model": 0}
        result = _apply_single_fix(
            finding=finding,
            worktree_path=git_repo,
            repo_path=git_repo,
            args=_args(tmp_path),
            log_file=tmp_path / "run.log",
            baseline_results=REPO_BASELINE,
            counters=counters,
        )
        assert result.status == "skipped"
        assert result.error == "not-llm-fixable"
        assert counters["deterministic"] == 0
        assert counters["model"] == 0

    def test_claude_engine_seeds_private_statuses_no_check_leak(
        self, tmp_path, make_finding, monkeypatch, git_repo
    ):
        import bluei.engine.lifecycle as lifecycle

        requests = []

        def _fake_claude(request):
            requests.append(request)
            return 0, "ok", None

        monkeypatch.setattr(lifecycle, "apply_claude_fix", _fake_claude)
        finding = make_finding(finding_id="f001", path="src/a.py")
        finding.safe_to_autofix = False
        args = _args(tmp_path)
        args.fix_engine = "claude"
        counters = {"attempts": 0, "deterministic": 0, "model": 0}
        # Unconfigured baseline results: fail WITHOUT launching the backend.
        result = _apply_single_fix(
            finding=finding, worktree_path=git_repo, repo_path=git_repo,
            args=args, log_file=tmp_path / "run.log",
            baseline_results=None, counters=counters,
        )
        assert result.status == "failed"
        assert result.error == "baseline-results-unconfigured"
        assert counters["model"] == 0
        assert requests == []
        # Configured baseline RESULTS (not the checks dict): launch counts
        # as a real model invocation; the request carries empty checks plus
        # the private rc statuses only — no check argv/names leak.
        result = _apply_single_fix(
            finding=finding, worktree_path=git_repo, repo_path=git_repo,
            args=args, log_file=tmp_path / "run.log",
            baseline_results=REPO_BASELINE, counters=counters,
        )
        assert counters["model"] == 1
        assert requests[0].baseline_checks == {}
        assert requests[0].baseline_statuses == [0]
        assert requests[0].allow_unchanged_baseline_failures is False
