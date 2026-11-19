"""Stage1 acceptance containment for unqualified repair lanes.

ACCEPTANCE-BOUNDARY-3ks3txvh: multi-finding batch repair and the
refactor-cycle lane are refused BEFORE any worktree, model, git, or
GitHub side effect. These tests prove the guards fire first by
monkeypatching every effectful entrypoint to raise if invoked.
"""

import argparse
from pathlib import Path

from bluei.engine.batch_pr import process_batch
from bluei.engine.commands.early_exits import run_refactor_cycle_command
from bluei.engine.models import BatchGroup

UNQUALIFIED_DETAIL = (
    "batch repair unqualified: missing configured scope/validation/closure acceptance"
)

# Every effectful call process_batch could reach: worktree lifecycle, the
# model-driven fix engine, git mutation, and GitHub I/O. Patched to raise.
_FORBIDDEN_TARGETS = [
    # resolved through the bluei.engine.batch_pr facade namespace
    "bluei.engine.batch_pr._create_worktree",
    "bluei.engine.batch_pr.hydrate_worktree",
    "bluei.engine.batch_pr.remove_worktree",
    "bluei.engine.batch_pr.apply_batch_fixes",
    "bluei.engine.batch_pr.handle_batch_failure",
    "bluei.engine.batch_pr.create_batch_pr",
    "bluei.engine.batch_pr.link_issues_to_batch_pr",
    # imported inside the (now unreachable) batch body
    "bluei.engine.gh.get_origin_url",
    "bluei.engine.gh.parse_github_repo",
    "bluei.engine.gh.find_batch_pr_by_rule",
    "bluei.engine.git_ops.git_commit_all",
    "bluei.engine.git_ops.git_push_branch",
]


def _forbid_effectful_calls(monkeypatch):
    """Patch every worktree/model/git/gh entrypoint to raise if invoked."""

    def _make_raiser(target):
        def _raiser(*args, **kwargs):
            raise AssertionError(
                f"containment violated: {target} was called by an "
                "unqualified repair lane"
            )

        return _raiser

    for target in _FORBIDDEN_TARGETS:
        monkeypatch.setattr(target, _make_raiser(target))


def _make_batch(make_finding, findings):
    issues = [
        {"finding_id": f.finding_id, "id": f"ISS-{i}"}
        for i, f in enumerate(findings)
    ]
    return BatchGroup(
        batch_id="batch-unqual-001",
        rule_pattern="ruff-c408",
        group_by="rule",
        findings=findings,
        issues=issues,
    )


def _batch_args(dry_run):
    return argparse.Namespace(dry_run=dry_run)


class TestProcessBatchUnqualifiedContainment:
    def test_multi_finding_batch_refused_before_any_worktree_model_or_gh_call(
        self, tmp_path, make_finding, monkeypatch
    ):
        _forbid_effectful_calls(monkeypatch)
        batch = _make_batch(
            make_finding,
            [
                make_finding(finding_id="f001", path="src/a.py"),
                make_finding(finding_id="f002", path="src/b.py"),
            ],
        )
        ok, detail = process_batch(batch, tmp_path / "repo", _batch_args(False), tmp_path / "run.log")
        assert ok is False
        assert detail == UNQUALIFIED_DETAIL

    def test_dry_run_reports_refusal_not_accepted_output(
        self, tmp_path, make_finding, monkeypatch
    ):
        _forbid_effectful_calls(monkeypatch)
        batch = _make_batch(
            make_finding,
            [
                make_finding(finding_id="f001", path="src/a.py"),
                make_finding(finding_id="f002", path="src/b.py"),
            ],
        )
        ok, detail = process_batch(batch, tmp_path / "repo", _batch_args(True), tmp_path / "run.log")
        # Dry-run must not report the accepted batch output either.
        assert ok is False
        assert detail == UNQUALIFIED_DETAIL
        assert detail != "dry-run-pr-simulated"

    def test_split_child_solo_sub_batch_also_refused(
        self, tmp_path, make_finding, monkeypatch
    ):
        _forbid_effectful_calls(monkeypatch)
        batch = _make_batch(make_finding, [make_finding(finding_id="f001")])
        ok, detail = process_batch(
            batch,
            tmp_path / "repo",
            _batch_args(False),
            tmp_path / "run.log",
            is_split_child=True,
        )
        assert ok is False
        assert detail == UNQUALIFIED_DETAIL

    def test_solo_delegation_preserved(self, tmp_path, make_finding, monkeypatch):
        _forbid_effectful_calls(monkeypatch)
        batch = _make_batch(make_finding, [make_finding(finding_id="f001")])
        ok, detail = process_batch(
            batch, tmp_path / "repo", _batch_args(False), tmp_path / "run.log"
        )
        assert ok is False
        assert detail == "solo-delegated"

    def test_refusal_does_not_mutate_batch_state(
        self, tmp_path, make_finding, monkeypatch
    ):
        _forbid_effectful_calls(monkeypatch)
        batch = _make_batch(
            make_finding,
            [
                make_finding(finding_id="f001", path="src/a.py"),
                make_finding(finding_id="f002", path="src/b.py"),
            ],
        )
        process_batch(batch, tmp_path / "repo", _batch_args(False), tmp_path / "run.log")
        assert batch.status == "open"
        assert batch.branch is None
        assert batch.worktree_path is None

    def test_refusal_reason_logged(self, tmp_path, make_finding, monkeypatch):
        _forbid_effectful_calls(monkeypatch)
        batch = _make_batch(
            make_finding,
            [
                make_finding(finding_id="f001", path="src/a.py"),
                make_finding(finding_id="f002", path="src/b.py"),
            ],
        )
        log_file = tmp_path / "run.log"
        process_batch(batch, tmp_path / "repo", _batch_args(False), log_file)
        assert "batch-refused" in log_file.read_text()
        assert UNQUALIFIED_DETAIL in log_file.read_text()


def _cycle_args(dry_run=True, auto_approve=True):
    return argparse.Namespace(
        run_phase="refactor-cycle",
        dry_run=dry_run,
        max_queue_items=5,
        auto_approve=auto_approve,
    )


class TestRunRefactorCycleCommandUnqualified:
    def test_unqualified_result_prints_marker_and_returns_2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            "bluei.engine.lifecycle.process_refactor_queue",
            lambda **kwargs: {
                "processed": [],
                "approved": [],
                "pending": ["w-1"],
                "failed": [],
                "unqualified": "refactor lane lacks closure acceptance",
            },
        )
        rc = run_refactor_cycle_command(
            repo_path=tmp_path / "repo",
            worktree_root=tmp_path / "wt",
            log_file=tmp_path / "run.log",
            args=_cycle_args(),
        )
        assert rc == 2
        out = capsys.readouterr().out
        assert "[UNQUALIFIED]" in out
        assert "refactor lane lacks closure acceptance" in out
        # No accepted-output summary for an unqualified run.
        assert "[DONE]" not in out

    def test_unqualified_result_logs_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "bluei.engine.lifecycle.process_refactor_queue",
            lambda **kwargs: {
                "processed": [],
                "approved": [],
                "pending": [],
                "failed": [],
                "unqualified": "missing validation acceptance",
            },
        )
        log_file = tmp_path / "run.log"
        run_refactor_cycle_command(
            repo_path=tmp_path / "repo",
            worktree_root=tmp_path / "wt",
            log_file=log_file,
            args=_cycle_args(),
        )
        assert "unqualified reason=missing validation acceptance" in log_file.read_text()

    def test_command_never_touches_queue_inventory_directly(self, tmp_path, monkeypatch):
        # The command must stay a pure consumer of process_refactor_queue's
        # result dict — no direct RefactorQueue construction or mutation.
        def _boom(*args, **kwargs):
            raise AssertionError(
                "containment violated: command touched RefactorQueue directly"
            )

        monkeypatch.setattr("bluei.engine.lifecycle.RefactorQueue", _boom)
        monkeypatch.setattr(
            "bluei.engine.lifecycle.process_refactor_queue",
            lambda **kwargs: {
                "processed": [],
                "approved": [],
                "pending": [],
                "failed": [],
                "unqualified": "read-only refusal",
            },
        )
        rc = run_refactor_cycle_command(
            repo_path=tmp_path / "repo",
            worktree_root=tmp_path / "wt",
            log_file=tmp_path / "run.log",
            args=_cycle_args(),
        )
        assert rc == 2

    def test_qualified_result_keeps_done_and_zero(self, tmp_path, monkeypatch, capsys):
        # Old contract (no 'unqualified' key) is unchanged.
        monkeypatch.setattr(
            "bluei.engine.lifecycle.process_refactor_queue",
            lambda **kwargs: {
                "processed": ["w-1"],
                "approved": ["w-1"],
                "pending": [],
                "failed": [],
            },
        )
        rc = run_refactor_cycle_command(
            repo_path=tmp_path / "repo",
            worktree_root=tmp_path / "wt",
            log_file=tmp_path / "run.log",
            args=_cycle_args(),
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "[DONE] refactor-cycle processed=1" in out
