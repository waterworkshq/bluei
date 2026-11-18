"""Candidate change boundary: what a dispatched model may do to the worktree.

``_process_one_issue`` captures HEAD before dispatch and rejects a candidate
that moved HEAD afterwards (committed behind the run's back), while
``diff_stats`` (``git diff --numstat HEAD``) counts STAGED changes so an
oversized stage-only candidate still hits the scope cap even though HEAD is
untouched. A candidate whose diff cannot be inspected (git failure) or contains
binary entries (``-`` numstat — the line-diff bound cannot be evaluated) raises
``ValueError`` out of ``diff_stats`` and ``_process_one_issue`` fails the
candidate closed (``candidate-diff-unavailable``) instead of scoring it (0, 0).
Helpers are reused from ``tests.test_final_acceptance_learning``
(same package). All gates are mocked qualified-pass, so the only live
rejection surface in the flow tests is the change boundary itself.
"""

from pathlib import Path

import pytest

from bluei.engine.commands.pr_cycle import _BreakLoop, _process_one_issue
from bluei.engine.git_ops import diff_stats
from tests.test_final_acceptance_learning import (
    _acceptance_spy,
    _args,
    _baseline_results,
    _ctx,
    _finding,
    _git,
    _git_repo,
    _harness,
    _issue,
)

_SRC = "src/main.py"
_TRANSPORT_OK = (0, "fixed", "prompt.txt")


def _patched_state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_diff_stats_counts_staged_candidate_and_ignores_committed_head(tmp_path):
    repo = _git_repo(tmp_path)
    target = repo / _SRC
    target.write_text(target.read_text() + "# extra\n" * 3)

    _git(repo, "add", _SRC)
    # Stage-only edits are visible to ``git diff --numstat HEAD``.
    assert diff_stats(repo) == (1, 3)

    # Once committed, the change is invisible to diff_stats — exactly why the
    # HEAD capture in _process_one_issue is needed alongside it.
    _git(repo, "commit", "-qm", "sneaky")
    assert diff_stats(repo) == (0, 0)


def test_staged_candidate_within_caps_retained_and_records(tmp_path):
    """Control: staging (HEAD untouched) is a LEGAL candidate shape. The
    boundary must let it through; the retained patch carries the staged bytes."""
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    def _model(req):
        target = req.worktree_path / _SRC
        target.write_text(
            target.read_text().replace("except:", "except Exception:")
        )
        _git(req.worktree_path, "add", _SRC)
        return _TRANSPORT_OK

    with _harness(repo, args) as harness:
        harness.transport.side_effect = _model
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(
                ctx, 0, issue, _finding(), _baseline_results()
            )

    assert deltas.fixes_verified == 1 and deltas.fixes_failed_verification == 0
    assert deltas.claude_invocations == 1
    assert deltas.blocked_reasons == []
    assert acc.spy.call_count == 1
    patch_path = Path(issue["verified_fix_patch"])
    assert patch_path.exists()
    retained = patch_path.read_bytes()
    assert retained.startswith(b"diff --git")
    assert b"except Exception:" in retained  # staged bytes are retained


def test_model_stage_bomb_over_cap_rejected_by_scope_limit(tmp_path):
    """Stage-only oversized candidate: HEAD unchanged, so only diff_stats
    (staged-aware) can catch it — the scope cap must reject it."""
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)  # max_loc_diff=200
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    def _model(req):
        target = req.worktree_path / _SRC
        target.write_text(target.read_text() + "# filler\n" * 250)
        _git(req.worktree_path, "add", _SRC)  # staged, never committed
        return _TRANSPORT_OK

    with _harness(repo, args) as harness:
        harness.transport.side_effect = _model
        with _acceptance_spy(issue) as acc:
            with pytest.raises(_BreakLoop) as excinfo:
                _process_one_issue(
                    ctx, 0, issue, _finding(), _baseline_results()
                )

    deltas = excinfo.value.args[0]
    harness.transport.assert_called_once()
    assert deltas.fixes_failed_verification == 1 and deltas.fixes_verified == 0
    assert "needs-human-scope-limit-exceeded" in deltas.blocked_reasons
    assert acc.spy.call_count == 0
    assert "verified_fix_patch" not in issue
    assert not list(_patched_state_dir(tmp_path).glob("*.patch"))


def test_model_commit_rejected_by_changed_head_boundary(tmp_path):
    """Committed candidate: diff_stats alone would report (0, 0) and mask it;
    the HEAD capture must reject it — no retained artifact, no positive record."""
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    def _model(req):
        target = req.worktree_path / _SRC
        target.write_text(
            target.read_text().replace("except:", "except Exception:")
        )
        _git(req.worktree_path, "add", _SRC)
        _git(req.worktree_path, "commit", "-qm", "sneaky model commit")
        return _TRANSPORT_OK

    with _harness(repo, args) as harness:
        harness.transport.side_effect = _model
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(
                ctx, 0, issue, _finding(), _baseline_results()
            )

    harness.transport.assert_called_once()
    assert deltas.fixes_failed_verification == 1 and deltas.fixes_verified == 0
    assert any(
        "candidate-changed-head" in reason for reason in deltas.blocked_reasons
    )
    assert issue["status"] == "fix_failed_verification"
    assert acc.spy.call_count == 0
    assert "verified_fix_patch" not in issue and "selected_fix_patch" not in issue
    assert not list(_patched_state_dir(tmp_path).glob("*.patch"))


def test_uninspectable_or_binary_candidate_fails_closed(tmp_path):
    """A candidate diff that cannot be measured is never scored (0, 0):
    diff_stats raises, _process_one_issue fails the candidate closed with
    ``candidate-diff-unavailable`` — no positive learning, no artifact."""
    # Raise contract: git failure (rc != 0, e.g. not a repository) → ValueError.
    with pytest.raises(ValueError, match="could not be inspected"):
        diff_stats(tmp_path)  # tmp_path itself is not a git repository

    # End-to-end: model stages a BINARY file (numstat reports ``-`` lines, so
    # the loc bound cannot be evaluated — the Stage 1 bound bypass). Real git,
    # HEAD untouched, within file caps: only diff_stats can refuse it.
    repo = _git_repo(tmp_path)
    args = _args(tmp_path)
    ctx = _ctx(tmp_path, repo, args)
    issue = _issue()

    def _model(req):
        (req.worktree_path / "payload.bin").write_bytes(b"\x00\x01binary\xff")
        _git(req.worktree_path, "add", "payload.bin")
        return _TRANSPORT_OK

    with _harness(repo, args) as harness:
        harness.transport.side_effect = _model
        with _acceptance_spy(issue) as acc:
            deltas = _process_one_issue(
                ctx, 0, issue, _finding(), _baseline_results()
            )

    harness.transport.assert_called_once()
    assert deltas.fixes_failed_verification == 1 and deltas.fixes_verified == 0
    assert any(
        "candidate-diff-unavailable" in reason for reason in deltas.blocked_reasons
    )
    assert issue["status"] == "fix_failed_verification"
    assert acc.spy.call_count == 0
    assert "verified_fix_patch" not in issue and "selected_fix_patch" not in issue
    assert not list(_patched_state_dir(tmp_path).glob("*.patch"))
