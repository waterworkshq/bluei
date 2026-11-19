"""Final acceptance learning boundary (A04): ordering tests for
``_process_one_issue`` on the direct model lane.

``record_accepted_fix`` is the ONLY positive learning emission point and runs
strictly at final outer acceptance: after scope, validation, closure AND
tracked-file patch retention (local/selected lanes) or after a successful
finalize WITH a PR number (live lane). A bare provider rc0 never learns.

These are ordering/boundary tests, not model efficacy tests: the "model" is a
fake backend executable driven through the real ``apply_claude_fix`` transport
(real prompt file, real ``bash -l -c`` subprocess) wherever the flow is meant
to succeed, so the retained patch bytes come from a real ``git diff --binary
HEAD`` in a real repository. ``run_validation_gate`` is mocked qualified-pass
per the approved boundary (the predispatch gate is additionally probed with an
explicit unqualified result), and ``run_named_checks`` supplies valid explicit
baseline entries with ``available=True`` (never empty). Publication is never
actually invoked: ``_finalize_pr_for_issue`` is mocked as the publication
stand-in on the live lane.
"""

import json
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from bluei.engine.commands.context import RunContext
from bluei.engine.commands.pr_cycle import (
    _BreakLoop,
    _process_one_issue,
    _WorktreeSetup,
)
from bluei.engine.models import Finding

FINDING_ID = "f001"

# Valid explicit runtime baseline evidence (available=True marker, not empty).
_BASELINE_ENTRY = {
    "rc": 0,
    "available": True,
    "unavailable_reason": None,
    "fingerprint": "",
}


def _baseline_results():
    return {"baseline-compile": dict(_BASELINE_ENTRY)}


_GATE_QUALIFIED = {"passed": True, "qualified": True, "message": "ok"}


# ── fixtures ─────────────────────────────────────────────────────────────────


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Test", "-c", "user.email=test@localhost",
            *argv,
        ],
        check=True,
        capture_output=True,
    )


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("try:\n    compute()\nexcept:\n    pass\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def _backend(tmp_path: Path, name: str, body: str) -> Path:
    """Fake model executable: driven through the real apply_claude_fix
    transport (bash -l -c, cwd=worktree), so the retained diff is real git."""
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


_FIXER_BODY = (
    "from pathlib import Path\n"
    "target = Path('src/main.py')\n"
    "target.write_text(target.read_text().replace('except:', 'except Exception:'))\n"
)
_NOOP_BODY = "import sys\nsys.exit(0)\n"


def _args(tmp_path: Path, backend: Path = None, **overrides) -> SimpleNamespace:
    defaults = dict(
        repo_path=str(tmp_path / "repo"),
        run_phase="pr-cycle",
        fix_engine="claude",
        finding_id=None,
        no_learning=False,
        live_github_actions=False,
        dry_run=False,
        allow_main_commit=False,
        force_push=False,
        max_prs_per_run=2,
        open_prs_cap=5,
        issue_confidence_threshold=0.5,
        max_fix_attempts_per_issue=3,
        batch_pr_enabled=False,
        deterministic_only=False,
        max_files_changed=5,
        max_loc_diff=200,
        allow_unchanged_baseline_failures=True,
        claude_cmd_template=(
            f"{backend} {{prompt_file}}" if backend else "unused-model {prompt_file}"
        ),
        pattern_store_path=None,
        batch_state_file=str(tmp_path / "batches.jsonl"),
        workspace=str(tmp_path),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _finding(**overrides) -> Finding:
    defaults = dict(
        finding_id=FINDING_ID,
        repo="test-repo",
        path="src/main.py",
        line=4,
        rule="broad-except",
        snippet="except:\n    pass",
        confidence=0.9,
        quick_win=False,
        safe_to_autofix=False,
        fix_attempts=0,
    )
    defaults.update(overrides)
    return Finding(**defaults)


def _issue() -> dict:
    return {
        "issue_id": "i1",
        "finding_id": FINDING_ID,
        "status": "open",
        "rule": "broad-except",
        "path": "src/main.py",
    }


def _ctx(tmp_path: Path, repo: Path, args: SimpleNamespace, **overrides) -> RunContext:
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "findings.jsonl").touch()
    lessons_file = tmp_path / "lessons.jsonl"
    lessons_file.touch()
    return RunContext(
        args=args,
        repo_path=repo,
        state_file=tmp_path / "state" / "run.json",
        log_file=tmp_path / "run.log",
        findings_file=tmp_path / "findings.jsonl",
        lessons_file=lessons_file,
        worktree_root=tmp_path / "worktrees",
        cost_tracker=MagicMock(
            exceeded_limit=Mock(return_value=False),
            cycle_total=Mock(return_value=0.0),
            estimate_invocation_cost=Mock(return_value=Decimal("0.05")),
            record_pattern_replay_savings=Mock(),
            record_invocation=Mock(),
        ),
        pattern_store=None,
        PER_REPO_BASELINE_CHECKS={"baseline-compile": [sys.executable, "-c", "pass"]},
        learning_mode=overrides.pop("learning_mode", "active"),
        **overrides,
    )


@contextmanager
def _acceptance_spy(issue: dict):
    """Spy on record_accepted_fix; snapshot retention state AT CALL TIME so
    ordering (bytes retained BEFORE record) is asserted, not assumed."""
    observed = []

    def _snap(req, *call_args, **call_kwargs):
        key = (
            "selected_fix_patch"
            if "selected_fix_patch" in issue
            else "verified_fix_patch"
        )
        path = issue.get(key)
        exists = bool(path) and Path(path).exists()
        observed.append(
            {
                "patch_key": key,
                "path": path,
                "exists": exists,
                "bytes_head": Path(path).read_bytes()[:64] if exists else None,
                "issue_status": issue.get("status"),
                "req": req,
            }
        )

    with patch(
        "bluei.engine.commands.pr_cycle.record_accepted_fix", side_effect=_snap
    ) as spy:
        yield SimpleNamespace(spy=spy, observed=observed)


def _gate_side_effect(gate):
    if isinstance(gate, list):
        return gate  # successive return values per gate invocation
    return lambda **kwargs: dict(gate)


@contextmanager
def _harness(
    repo: Path,
    args: SimpleNamespace,
    *,
    gate=_GATE_QUALIFIED,
    verify_closed=True,
    stats=None,
    real_transport=False,
):
    """Neutralize resolution/worktree/publication; keep git + counters real.

    Yields a namespace with the ExitStack (for extra patches) and, when the
    transport is mocked, the ``apply_claude_fix`` mock for call assertions.
    """
    with ExitStack() as stack:
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle._resolve_github", return_value=False)
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle._setup_worktree",
                return_value=_WorktreeSetup(
                    worktree_path=repo, worktree_branch="fix/test"
                ),
            )
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle.run_named_checks",
                side_effect=lambda **kwargs: _baseline_results(),
            )
        )
        if gate is not None:
            stack.enter_context(
                patch(
                    "bluei.engine.commands.pr_cycle.run_validation_gate",
                    side_effect=_gate_side_effect(gate),
                )
            )
        if stats is not None:
            stack.enter_context(
                patch("bluei.engine.commands.pr_cycle.diff_stats", return_value=stats)
            )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle.verify_fix_closed",
                return_value=SimpleNamespace(is_closed=verify_closed),
            )
        )
        stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle.load_authoritative_guidelines",
                return_value=None,
            )
        )
        stack.enter_context(
            patch("bluei.engine.commands.pr_cycle.load_repo_taste", return_value=None)
        )
        stack.enter_context(patch("bluei.engine.commands.pr_cycle.remove_worktree"))
        # Governor resolution is external config I/O, not acceptance ordering.
        stack.enter_context(
            patch(
                "bluei.engine.model_governor.resolve_governed_model",
                return_value=(None, None, args.claude_cmd_template, "governor-test"),
            )
        )
        transport = None
        if not real_transport:
            transport = stack.enter_context(
                patch(
                    "bluei.engine.commands.pr_cycle.apply_claude_fix",
                    return_value=(0, "fixed", "prompt.txt"),
                )
            )
        yield SimpleNamespace(stack=stack, transport=transport)


def _capturing_transport(stack: ExitStack):
    """Wrap (not replace) apply_claude_fix so the REAL transport runs while
    capturing the dispatched ClaudeFixRequest for identity assertions."""
    from bluei.engine.commands import pr_cycle as pr_cycle_module

    real_apply = pr_cycle_module.apply_claude_fix
    seen = []

    def _capture(req):
        seen.append(req)
        return real_apply(req)

    stack.enter_context(
        patch("bluei.engine.commands.pr_cycle.apply_claude_fix", side_effect=_capture)
    )
    return seen


# ── accepted: real transport, real git retention, THEN record once ───────────


def test_accepted_local_ordinary_retains_real_diff_bytes_then_records_once(tmp_path):
    repo = _git_repo(tmp_path)
    backend = _backend(tmp_path, "fixer", _FIXER_BODY)
    args = _args(tmp_path, backend, live_github_actions=False, dry_run=False)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    with _harness(repo, args, real_transport=True) as harness:
        seen = _capturing_transport(harness.stack)
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    assert deltas.claude_invocations == 1 and deltas.fixes_verified == 1
    # Local ordinary lane: verified_fix_patch key, issue stays open.
    assert "selected_fix_patch" not in issue
    patch_path = Path(issue["verified_fix_patch"])
    assert issue["status"] == "open"
    assert patch_path.exists()
    assert patch_path.name.startswith("local-fix-")
    retained = patch_path.read_bytes()
    assert retained.startswith(b"diff --git")
    assert b"except Exception:" in retained
    # The retained artifact is byte-identical to the worktree's real diff.
    expected = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "HEAD"],
        capture_output=True,
        check=True,
    ).stdout
    assert retained == expected

    # Exactly one record, carrying the request the transport actually got.
    assert acc.spy.call_count == 1
    snap = acc.observed[0]
    assert snap["req"] is seen[0]
    assert snap["req"].lessons_file == ctx.lessons_file
    # Ordering: at record time the patch was already retained and the issue
    # already re-opened — retention strictly precedes the positive record.
    assert snap["exists"] is True
    assert snap["bytes_head"].startswith(b"diff --git")
    assert snap["issue_status"] == "open"
    assert "patch-verified" in json.dumps(ctx.state.get("finding_activity", {}))


def test_accepted_selected_repair_retains_real_diff_bytes_then_records_once(tmp_path):
    repo = _git_repo(tmp_path)
    backend = _backend(tmp_path, "fixer", _FIXER_BODY)
    args = _args(tmp_path, backend, finding_id=FINDING_ID)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    with _harness(repo, args, real_transport=True) as harness:
        seen = _capturing_transport(harness.stack)
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    assert deltas.fixes_verified == 1
    # Selected lane keeps the selected_fix_patch key; issue stays open.
    assert "verified_fix_patch" not in issue
    patch_path = Path(issue["selected_fix_patch"])
    assert issue["status"] == "open"
    assert patch_path.exists()
    assert patch_path.name.startswith("selected-fix-")
    assert patch_path.read_bytes().startswith(b"diff --git")

    assert acc.spy.call_count == 1
    snap = acc.observed[0]
    assert snap["patch_key"] == "selected_fix_patch"
    assert snap["exists"] is True
    assert snap["issue_status"] == "open"
    assert snap["req"] is seen[0]


@pytest.mark.parametrize(
    "ctx_overrides,args_overrides",
    [
        ({"learning_mode": "paused"}, {}),
        ({}, {"no_learning": True}),
    ],
    ids=["paused-learning-mode", "no-learning-flag"],
)
def test_paused_or_no_learning_nulls_learning_sinks_but_records_once(
    tmp_path, ctx_overrides, args_overrides
):
    repo = _git_repo(tmp_path)
    backend = _backend(tmp_path, "fixer", _FIXER_BODY)
    args = _args(tmp_path, backend, **args_overrides)
    ctx = _ctx(tmp_path, repo, args, **ctx_overrides)
    issue = _issue()

    with _harness(repo, args, real_transport=True) as harness:
        _capturing_transport(harness.stack)
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    # Acceptance still records exactly once, but the learning sinks on the
    # recorded request are nulled — no lessons/pattern writes, while attempt
    # accounting (findings_file) stays live.
    assert acc.spy.call_count == 1
    req = acc.observed[0]["req"]
    assert req.lessons_file is None
    assert req.pattern_store_path is None
    assert req.findings_file == ctx.findings_file
    assert deltas.fixes_verified == 1
    assert Path(issue["verified_fix_patch"]).exists()
    assert issue["status"] == "open"


# ── zero-exit candidates rejected by the outer gates never record ────────────


def test_zero_exit_candidate_scope_rejected_never_records(tmp_path):
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    with _harness(repo, args, stats=(99, 9999)):
        with _acceptance_spy(issue) as acc:
            with pytest.raises(_BreakLoop):
                _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    assert acc.spy.call_count == 0
    assert "verified_fix_patch" not in issue
    assert not list((tmp_path / "state").glob("*.patch"))


def test_zero_exit_candidate_validation_rejected_never_records(tmp_path):
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    # Gate sequence: predispatch qualified-pass, FINAL gate failed.
    gate_sequence = [
        dict(_GATE_QUALIFIED),
        {
            "passed": False,
            "qualified": True,
            "message": "regression in: baseline-compile",
        },
    ]
    with _harness(repo, args, gate=gate_sequence, stats=(1, 4)):
        with _acceptance_spy(issue) as acc:
            with pytest.raises(_BreakLoop):
                _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    assert acc.spy.call_count == 0
    assert "verified_fix_patch" not in issue
    assert not list((tmp_path / "state").glob("*.patch"))


def test_zero_exit_candidate_closure_rejected_never_records(tmp_path):
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    with _harness(repo, args, stats=(1, 4), verify_closed=False):
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    assert acc.spy.call_count == 0
    assert deltas.fixes_failed_verification == 1 and deltas.fixes_verified == 0
    assert "verified_fix_patch" not in issue
    assert not list((tmp_path / "state").glob("*.patch"))


def test_zero_exit_candidate_retention_rejected_never_records(tmp_path):
    repo = _git_repo(tmp_path)  # unmodified: the real `git diff` is empty
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    with _harness(repo, args, stats=(1, 4)):
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    # The real retention block ran and rejected the empty real diff.
    assert acc.spy.call_count == 0
    assert deltas.fixes_failed_verification == 1
    assert "repair patch could not be retained" in deltas.blocked_reasons
    assert "verified_fix_patch" not in issue
    assert not list((tmp_path / "state").glob("*.patch"))


def test_predispatch_unqualified_gate_blocks_before_model_dispatch_never_records(
    tmp_path,
):
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    # passed=True alone is NOT acceptance evidence: the predispatch gate
    # requires passed AND qualified.
    unqualified = {
        "passed": True,
        "qualified": False,
        "message": "baseline checks unqualified",
    }
    with _harness(repo, args, gate=unqualified) as harness:
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    harness.transport.assert_not_called()
    assert acc.spy.call_count == 0
    assert deltas.fix_attempts == 1 and deltas.claude_invocations == 0
    assert deltas.fixes_failed_verification == 1
    assert any("unqualified" in reason for reason in deltas.blocked_reasons)


def test_noop_zero_exit_is_not_positive_never_records(tmp_path):
    repo = _git_repo(tmp_path)
    backend = _backend(tmp_path, "noop-model", _NOOP_BODY)
    args = _args(tmp_path, backend)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    # Real transport (rc0, no changes) + REAL diff_stats → genuine (0, 0);
    # the finding is already closed on the branch → resolved-without-changes.
    with _harness(repo, args, real_transport=True) as harness:
        _capturing_transport(harness.stack)
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    assert acc.spy.call_count == 0
    assert deltas.fixes_verified == 0
    assert "verified_fix_patch" not in issue
    assert not list((tmp_path / "state").glob("*.patch"))
    assert "resolved-noop-verified" in json.dumps(ctx.state.get("finding_activity", {}))


# ── live lane: record only on finalize success WITH a PR number ──────────────


def _finalize_result(**overrides):
    result = SimpleNamespace(
        success=True,
        pr_number=7,
        run_status="pr-created",
        should_break=False,
        fixes_verified_delta=1,
        fixes_failed_verification_delta=0,
        created_prs_delta=1,
        open_prs_delta=1,
        blocked_reasons_additions=[],
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


@pytest.mark.parametrize(
    "finalize_kwargs,expected_records",
    [
        ({"success": True, "pr_number": 7}, 1),
        ({"success": True, "pr_number": None}, 0),
        ({"success": False, "pr_number": None}, 0),
    ],
    ids=["success-with-pr", "success-without-pr", "finalize-failure"],
)
def test_live_lane_records_only_on_finalize_success_with_pr_number(
    tmp_path, finalize_kwargs, expected_records
):
    repo = _git_repo(tmp_path)
    args = _args(tmp_path, live_github_actions=True, dry_run=False)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    with _harness(repo, args, stats=(1, 4)) as harness:
        finalize = harness.stack.enter_context(
            patch(
                "bluei.engine.commands.pr_cycle._finalize_pr_for_issue",
                return_value=_finalize_result(**finalize_kwargs),
            )
        )
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(ctx, 0, issue, _finding(), _baseline_results())

    # Publication is never actually invoked: the finalize stand-in is the only
    # publication surface touched, exactly once.
    finalize.assert_called_once()
    assert acc.spy.call_count == expected_records
    assert "verified_fix_patch" not in issue
    assert "selected_fix_patch" not in issue
    assert not list((tmp_path / "state").glob("*.patch"))
    if expected_records:
        # The recorded request is exactly the one handed to the transport.
        assert acc.observed[0]["req"] is harness.transport.call_args.args[0]
        assert deltas.created_prs == 1
