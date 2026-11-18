"""Adjacent-lane acceptance regressions (Stage 3, LANE-ACCEPTANCE-zhy98vs7).

Covers the campaign and review lanes' parity with the solo acceptance
contract: qualified configured baseline checks (no inference fallback),
per-fix candidate acceptance (scope caps, stable HEAD, closure), durable
patch retention before counting, full source-checkpoint rollback, and
pending-push revalidation semantics.

Existing tests that asserted the removed campaign pattern-replay branch
(tests/test_campaign_executor.py) are intentionally untouched here; their
disposition belongs to the parent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bluei.campaigns.executor import CampaignExecutor, autofix_fix_runner
from bluei.campaigns.planner import Campaign, CampaignPhase
from bluei.campaigns.state import CampaignStateManager
from bluei.campaigns.types import CampaignStatus
from bluei.common.models import Repo, RepoConfig
from bluei.review.cycle import ReviewCycleEngine


PASSING_CHECK = ["python3", "-c", "pass"]


def _commit_all(repo_path: Path, message: str = "init") -> None:
    subprocess.run(
        ["git", "add", "."], cwd=str(repo_path), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
    )


def _make_git_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    for args in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(args, cwd=str(worktree), capture_output=True, check=True)
    (worktree / "app.py").write_text("print('broken')\n", encoding="utf-8")
    (worktree / "extra.py").write_text("x = 1\n", encoding="utf-8")
    (worktree / "sentinel.txt").write_text("clean\n", encoding="utf-8")
    _commit_all(worktree)
    return worktree


def _campaign_with_one_finding(finding_dict: dict) -> Campaign:
    phase = CampaignPhase(
        phase_id="phase-0", index=0, title="t", finding_ids=["f1"]
    )
    return Campaign(
        campaign_id="camp-test-00000001-abcdef12",
        repo="r",
        title="t",
        description="d",
        strategy="rule_based",
        target_rules=[],
        target_paths=[],
        target_findings=["f1"],
        finding_snapshots={"f1": finding_dict},
        phases=[phase],
        dependency_graph={},
    )


def _review_engine(repo: Repo) -> ReviewCycleEngine:
    engine = ReviewCycleEngine.__new__(ReviewCycleEngine)
    engine.repo = repo
    return engine


def _review_repo(tmp_path: Path, **overrides) -> Repo:
    repo_path = tmp_path / "repo"
    repo_path.mkdir(exist_ok=True)
    kwargs = dict(id="repo-test", name="test-repo", path=str(repo_path),
                  language="typescript")
    kwargs.update(overrides)
    return Repo(config=RepoConfig(**kwargs))


# --------------------------------------------------------------------------
# Campaign lane
# --------------------------------------------------------------------------


def test_mutating_run_without_baseline_checks_refused_unqualified(
    tmp_path, make_finding
):
    worktree = _make_git_worktree(tmp_path)
    store = CampaignStateManager(tmp_path / "campaigns")
    store.save(_campaign_with_one_finding(make_finding(path="app.py").to_dict()))
    runner_calls = []

    def fix_runner(**kwargs):
        runner_calls.append(kwargs)
        return {"status": "success", "method": "fix_runner"}

    result = CampaignExecutor(
        store, fix_runner=fix_runner
    ).run(
        "camp-test-00000001-abcdef12",
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert result["unqualified"] == "no registered baseline checks configured"
    assert result["status"] == CampaignStatus.paused
    assert runner_calls == []
    assert (worktree / "app.py").read_text() == "print('broken')\n"
    campaign = store.load("camp-test-00000001-abcdef12")
    assert campaign.pause_reason == "unqualified-baseline"


def test_accepted_fix_retains_patch_before_counting(tmp_path, make_finding, monkeypatch):
    from bluei.engine import repair_acceptance

    worktree = _make_git_worktree(tmp_path)
    store = CampaignStateManager(tmp_path / "campaigns")
    store.save(_campaign_with_one_finding(make_finding(path="app.py").to_dict()))

    monkeypatch.setattr(
        repair_acceptance,
        "verify_fix_closed",
        lambda *args, **kwargs: SimpleNamespace(is_closed=True),
        raising=True,
    )

    def fix_runner(**kwargs):
        (worktree / "app.py").write_text("print('fixed')\n", encoding="utf-8")
        return {"status": "success", "method": "fix_runner"}

    result = CampaignExecutor(
        store,
        fix_runner=fix_runner,
        baseline_checks={"baseline-0": PASSING_CHECK},
    ).run(
        "camp-test-00000001-abcdef12",
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert result["status"] == CampaignStatus.completed
    assert result["findings_fixed"] == 1
    patches = list(
        (tmp_path / "campaigns" / "camp-test-00000001-abcdef12" / "patches").glob(
            "campaign-fix-*.patch"
        )
    )
    assert len(patches) == 1
    assert b"print('fixed')" in patches[0].read_bytes()
    fixed_events = [
        event
        for event in store.events("camp-test-00000001-abcdef12", limit=50)
        if event.get("event") == "finding.fixed"
    ]
    assert fixed_events and fixed_events[0]["payload"]["patch"].endswith(".patch")


def test_rejected_candidate_rolls_back_all_touched_paths(
    tmp_path, make_finding, monkeypatch
):
    from bluei.engine import repair_acceptance

    worktree = _make_git_worktree(tmp_path)
    store = CampaignStateManager(tmp_path / "campaigns")
    store.save(_campaign_with_one_finding(make_finding(path="app.py").to_dict()))

    # The registered check passes at baseline but the fix breaks it, so the
    # per-fix candidate gate must reject and roll the whole phase back.
    guard = worktree / "guard.py"
    guard.write_text(
        'import sys\nfrom pathlib import Path\n'
        'raise SystemExit(0 if Path("sentinel.txt").read_text() == "clean\\n" else 1)\n',
        encoding="utf-8",
    )
    _commit_all(worktree, message="add guard")

    monkeypatch.setattr(
        repair_acceptance,
        "verify_fix_closed",
        lambda *args, **kwargs: SimpleNamespace(is_closed=True),
    )

    def fix_runner(**kwargs):
        (worktree / "app.py").write_text("print('fixed')\n", encoding="utf-8")
        # Out-of-finding-path modification: finding-path-only rollback would
        # leave this behind; the source checkpoint must restore it.
        (worktree / "extra.py").write_text("x = 2\n", encoding="utf-8")
        (worktree / "sentinel.txt").write_text("broken\n", encoding="utf-8")
        return {"status": "success", "method": "fix_runner"}

    result = CampaignExecutor(
        store,
        fix_runner=fix_runner,
        baseline_checks={"baseline-0": ["python3", str(guard)]},
    ).run(
        "camp-test-00000001-abcdef12",
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert result["status"] == CampaignStatus.paused
    assert result["findings_fixed"] == 0
    assert (worktree / "app.py").read_text() == "print('broken')\n"
    assert (worktree / "extra.py").read_text() == "x = 1\n"
    assert (worktree / "sentinel.txt").read_text() == "clean\n"
    campaign = store.load("camp-test-00000001-abcdef12")
    assert campaign.findings_fixed == 0
    rollback_events = [
        event
        for event in store.events("camp-test-00000001-abcdef12", limit=50)
        if event.get("event") == "phase.rollback.applied"
    ]
    assert rollback_events and rollback_events[0]["payload"]["mode"] == "source-checkpoint"


def test_unretainable_patch_is_not_counted(tmp_path, make_finding, monkeypatch):
    from bluei.campaigns import executor as executor_module
    from bluei.engine import repair_acceptance

    worktree = _make_git_worktree(tmp_path)
    store = CampaignStateManager(tmp_path / "campaigns")
    store.save(_campaign_with_one_finding(make_finding(path="app.py").to_dict()))

    monkeypatch.setattr(
        repair_acceptance,
        "verify_fix_closed",
        lambda *args, **kwargs: SimpleNamespace(is_closed=True),
    )
    monkeypatch.setattr(
        executor_module,
        "retain_candidate_patch",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("repair patch is empty")),
    )

    def fix_runner(**kwargs):
        (worktree / "app.py").write_text("print('fixed')\n", encoding="utf-8")
        return {"status": "success", "method": "fix_runner"}

    result = CampaignExecutor(
        store,
        fix_runner=fix_runner,
        baseline_checks={"baseline-0": PASSING_CHECK},
    ).run(
        "camp-test-00000001-abcdef12",
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )

    assert result["status"] == CampaignStatus.paused
    assert result["findings_fixed"] == 0
    assert (worktree / "app.py").read_text() == "print('broken')\n"


def test_campaign_fix_runner_never_replays_patterns(tmp_path, make_finding):
    src_file = tmp_path / "app.py"
    src_file.write_text("old_code()\n", encoding="utf-8")
    store_path = tmp_path / "fake_store.jsonl"
    store_path.write_text("{}\n", encoding="utf-8")

    mock_store = MagicMock()
    mock_pattern = MagicMock()
    mock_pattern.confidence = 0.95
    mock_pattern.before_snippet = "old_code()"
    mock_pattern.after_snippet = "new_code()"
    mock_store.lookup.return_value = mock_pattern

    from bluei.campaigns import executor as executor_module

    with patch.object(
        executor_module, "FixPatternStore", return_value=mock_store
    ) as store_cls:
        result = autofix_fix_runner(
            campaign=MagicMock(),
            phase=MagicMock(),
            finding_id="f1",
            finding=make_finding(path="app.py", snippet="old_code()").to_dict(),
            worktree_path=tmp_path,
            pattern_store_path=store_path,
        )

    assert store_cls.call_count == 0
    assert result.get("method") != "pattern_replay"
    assert src_file.read_text() == "old_code()\n"


# --------------------------------------------------------------------------
# Review lane
# --------------------------------------------------------------------------


def test_review_validation_has_no_inference_fallback(tmp_path):
    engine = _review_engine(_review_repo(tmp_path, baseline_checks=[]))
    # Enabled-path wiring would provide an inferrer; it must not be used.
    engine.baseline_inferrer = lambda repo_path, language: [["python3", "-c", "pass"]]
    result = engine._run_validation(tmp_path)
    assert result["ok"] is False
    assert result["reason"] == "unqualified: no-validation-configured"


def test_review_push_boundary_blocks_over_scope(tmp_path):
    worktree = _make_git_worktree(tmp_path)
    repo = _review_repo(tmp_path)
    engine = _review_engine(repo)
    changed_files = []
    for index in range(6):
        name = f"file{index}.py"
        (worktree / name).write_text(f"x = {index}\n", encoding="utf-8")
        changed_files.append(name)
    _commit_all(worktree, message="six files")
    for index in range(6):
        (worktree / f"file{index}.py").write_text(
            f"x = {index}\ny = {index}\n", encoding="utf-8"
        )

    result = engine._apply_commit_push_boundary(
        worktree,
        {"pr_number": 1, "branch": "qa/fix-1"},
        changed_files,
        allow_review_push=True,
    )
    assert result["status"] == "blocked_by_scope"
    assert result["scope_reason"].startswith("scope-limit-exceeded")
    # Nothing was committed.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=str(worktree),
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert len(log) == 2  # init + six files


def test_review_backend_head_change_rejected(tmp_path):
    worktree = _make_git_worktree(tmp_path)
    engine = _review_engine(_review_repo(tmp_path))
    validation_called = []

    def fake_shell(command, cwd, timeout_seconds=900):
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "backend-moved-head"],
            cwd=str(cwd), capture_output=True, check=True,
        )
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}

    engine._run_shell = fake_shell
    engine._collect_changed_files = lambda cwd: ["app.py"]
    engine._run_validation = lambda cwd: (
        validation_called.append(1) or {"ok": True, "results": [], "reason": "completed"}
    )
    engine._apply_commit_push_boundary = MagicMock()

    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "x",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=True,
        snapshot={"pr_number": 1, "branch": "qa/fix-1"},
    )
    assert result["status"] == "retry_failed"
    assert result["head_changed"] is True
    assert result["validation"]["reason"] == "head-changed-by-backend"
    assert result["push_result"] is None
    engine._apply_commit_push_boundary.assert_not_called()


def test_pending_push_revalidated_without_backend(tmp_path):
    worktree = _make_git_worktree(tmp_path)
    (worktree / "app.py").write_text("print('fixed')\n", encoding="utf-8")
    engine = _review_engine(_review_repo(tmp_path))
    engine.provider = SimpleNamespace(repo_path=worktree)
    shell_calls = []

    def fake_shell(command, cwd, timeout_seconds=900):
        shell_calls.append(command)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}

    engine._run_shell = fake_shell
    engine._run_validation = lambda cwd: {"ok": True, "results": [], "reason": "completed"}
    boundary_calls = []

    def fake_boundary(worktree_path, snapshot, changed_files, allow_review_push):
        boundary_calls.append((changed_files, allow_review_push))
        return {
            "status": "pending_operator_confirmation",
            "allow_review_push": allow_review_push,
        }

    engine._apply_commit_push_boundary = fake_boundary

    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "x",
        },
        {
            "attempts_used": 1,
            "last_action": "retry_pending_push",
            "execution_result": {
                "status": "retry_pending_push",
                "changed_files": ["app.py"],
            },
        },
        dry_run=False,
        allow_review_push=False,
        snapshot={"pr_number": 1, "branch": "qa/fix-1"},
    )
    assert shell_calls == []
    assert result["revalidated"] is True
    assert result["executed"] is False
    assert result["attempts_used"] == 1  # no attempt burned
    assert result["status"] == "retry_pending_push"
    assert boundary_calls == [(["app.py"], False)]


def test_pending_push_falls_back_to_fresh_attempt_when_changes_lost(tmp_path):
    worktree = _make_git_worktree(tmp_path)  # clean worktree: changes gone
    engine = _review_engine(_review_repo(tmp_path))
    engine.provider = SimpleNamespace(repo_path=worktree)
    shell_calls = []

    def fake_shell(command, cwd, timeout_seconds=900):
        shell_calls.append(command)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}

    engine._run_shell = fake_shell
    engine._run_validation = lambda cwd: {"ok": True, "results": [], "reason": "completed"}

    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "x",
        },
        {
            "attempts_used": 1,
            "last_action": "retry_pending_push",
            "execution_result": {
                "status": "retry_pending_push",
                "changed_files": ["app.py"],
            },
        },
        dry_run=False,
        allow_review_push=False,
        snapshot={"pr_number": 1, "branch": "qa/fix-1"},
    )
    assert shell_calls  # fresh backend attempt was made
    assert result["executed"] is True
    assert result["attempts_used"] == 2
    assert result["status"] == "retry_no_changes"


def test_prepare_worktree_preserves_dirty_pending_push(tmp_path, monkeypatch):
    from bluei import review

    dirty_dir = tmp_path / "review-pr-1"
    dirty_dir.mkdir()
    (dirty_dir / "app.py").write_text("pending\n", encoding="utf-8")
    engine = _review_engine(_review_repo(tmp_path))
    engine.provider = SimpleNamespace(repo_path=tmp_path)
    engine._review_worktree_path = lambda pr_number: dirty_dir
    monkeypatch.setattr(review.worktree, "get_worktree_branch", lambda path: "qa-review-pr-1")
    commands = []

    def fake_run_repo_cmd(cmd, cwd=None, check=True):
        commands.append(list(cmd))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return " M app.py"
        return ""

    engine._run_repo_cmd = fake_run_repo_cmd

    preserved = engine._prepare_worktree(
        {"pr_number": 1, "branch": "qa/fix-1"}, dry_run=False, preserve_dirty=True
    )
    assert preserved["prepared"] is True
    assert preserved.get("preserved_dirty") is True
    assert (dirty_dir / "app.py").read_text() == "pending\n"
    assert not any("worktree" in " ".join(c) and "remove" in " ".join(c) for c in commands)

    recreated = engine._prepare_worktree(
        {"pr_number": 1, "branch": "qa/fix-1"}, dry_run=False, preserve_dirty=False
    )
    assert any("remove" in " ".join(c) for c in commands)
    assert recreated.get("preserved_dirty") is None


# --------------------------------------------------------------------------
# Fail-closed HEAD preconditions (F1/F4) and registered cap propagation (F2)
# --------------------------------------------------------------------------


def test_review_head_unavailable_fails_closed_before_backend(tmp_path):
    """A worktree whose HEAD cannot be resolved refuses remediation up front.

    The head gate must be a precondition: an empty head_before would
    otherwise silently skip the head-change check (fail-open).
    """
    worktree = tmp_path / "not-a-git-dir"
    worktree.mkdir()
    engine = _review_engine(_review_repo(tmp_path))
    shell_calls = []
    engine._run_shell = (
        lambda command, cwd, timeout_seconds=900: shell_calls.append(command)
        or {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
    )
    engine._apply_commit_push_boundary = MagicMock()

    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(worktree)},
            "backend_command": "x",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=True,
        snapshot={"pr_number": 1, "branch": "qa/fix-1"},
    )
    assert result["status"] == "retry_failed"
    assert result["validation"]["reason"] == "head-unavailable"
    assert shell_calls == []  # backend never ran
    engine._apply_commit_push_boundary.assert_not_called()


def test_review_missing_worktree_oserror_fails_closed(tmp_path):
    """A vanished worktree (OSError from git spawn) fails closed, no crash."""
    engine = _review_engine(_review_repo(tmp_path))
    shell_calls = []
    engine._run_shell = (
        lambda command, cwd, timeout_seconds=900: shell_calls.append(command)
        or {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False}
    )

    result = engine._execute_prepared_remediation(
        {
            "status": "retry_prepared",
            "worktree": {"worktree_path": str(tmp_path / "vanished")},
            "backend_command": "x",
        },
        {"attempts_used": 0},
        dry_run=False,
        allow_review_push=True,
        snapshot={"pr_number": 1, "branch": "qa/fix-1"},
    )
    assert result["status"] == "retry_failed"
    assert result["validation"]["reason"] == "head-unavailable"
    assert shell_calls == []


def test_registered_lower_cap_propagates_and_gates(tmp_path, make_finding):
    """Registered limits are the real campaign scope caps — not optional.

    A registered max_files_changed of 1 (lower than the product default 5)
    must reach the executor and reject a two-file candidate.
    """
    from bin.cmd_campaign import _load_campaign_check_source

    registered = SimpleNamespace(
        baseline_checks=[["git", "diff", "--check", "HEAD", "--"]],
        path=str(tmp_path),
        limits={"max_files_changed": 1, "max_loc_diff": 50},
    )
    config_manager = SimpleNamespace(
        load_repo_config=lambda repo: registered if repo == "demo" else None
    )
    checks, source, limits = _load_campaign_check_source(config_manager, "demo")
    assert checks == {"baseline-0": ["git", "diff", "--check", "HEAD", "--"]}
    assert source == Path(str(tmp_path))
    assert limits == {"max_files_changed": 1, "max_loc_diff": 50}

    # The CLI wiring translates the registered limits into executor caps;
    # with cap 1 a two-file candidate is rejected and rolled back.
    worktree = _make_git_worktree(tmp_path)
    (worktree / "second.py").write_text("y = 1\n", encoding="utf-8")
    _commit_all(worktree, message="second file")
    store = CampaignStateManager(tmp_path / "campaigns")
    store.save(_campaign_with_one_finding(make_finding(path="app.py").to_dict()))

    def fix_runner(**kwargs):
        (worktree / "app.py").write_text("print('fixed')\n", encoding="utf-8")
        (worktree / "second.py").write_text("y = 2\n", encoding="utf-8")
        return {"status": "success", "method": "fix_runner"}

    result = CampaignExecutor(
        store,
        fix_runner=fix_runner,
        baseline_checks=checks,
        max_files_changed=int(limits.get("max_files_changed", 5)),
        max_loc_diff=int(limits.get("max_loc_diff", 200)),
    ).run(
        "camp-test-00000001-abcdef12",
        dry_run=False,
        worktree_path=worktree,
        require_clean_worktree=True,
    )
    assert result["status"] == CampaignStatus.paused
    assert result["findings_fixed"] == 0
    assert (worktree / "app.py").read_text() == "print('broken')\n"
    assert (worktree / "second.py").read_text() == "y = 1\n"
