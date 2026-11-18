"""Real Git/check controls for the minimum cross-lane candidate contract."""

import subprocess
import sys

import pytest

from bluei.engine.models import Finding
from bluei.engine import repair_acceptance as acceptance


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "add", "app.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture")
    return repo


def _finding(identity):
    return Finding(finding_id=identity, repo="fixture", rule="ruff-b904", path="app.py",
                   line=1, snippet="diagnostic", confidence=0.95,
                   safe_to_autofix=True, quick_win=True)


def _candidate(repo, tmp_path, findings=None, checks=None):
    checks = checks if checks is not None else {"control": [sys.executable, "-c", "assert True"]}
    baseline, gate = acceptance.prepare_baseline(
        worktree_path=repo, source_repo=repo, checks=checks, repo_baseline_results={},
        allow_unchanged_baseline_failures=True, log_file=tmp_path / "run.log",
    )
    return dict(worktree_path=repo, source_repo=repo,
                candidate_base=_git(repo, "rev-parse", "HEAD").stdout.decode().strip(),
                findings=findings or [_finding("one")], checks=checks,
                baseline_results=baseline, allow_unchanged_baseline_failures=True,
                max_files_changed=1, max_loc_diff=10, log_file=tmp_path / "run.log"), gate


def test_batch_requires_every_finding_closed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    repo = _repo(tmp_path)
    kwargs, gate = _candidate(repo, tmp_path, [_finding("one"), _finding("two")])
    assert gate["passed"] and gate["qualified"]
    (repo / "app.py").write_text("value = 2\n")
    observed = []

    def closure(worktree, finding, log_file, **options):
        observed.append(finding.finding_id)
        return SimpleNamespace(is_closed=finding.finding_id == "one")

    monkeypatch.setattr(acceptance, "verify_fix_closed", closure)
    verdict = acceptance.validate_candidate(**kwargs)
    assert not verdict["passed"]
    assert verdict["message"] == "detector-still-firing"
    assert observed == ["one", "two"]


@pytest.mark.parametrize("kind", ["empty", "missing", "failing"])
def test_unqualified_baseline_never_accepts_candidate(tmp_path, kind):
    repo = _repo(tmp_path)
    checks = {"empty": {}, "missing": {"control": ["/absent/check"]},
              "failing": {"control": [sys.executable, "-c", "raise SystemExit(1)"]}}[kind]
    kwargs, gate = _candidate(repo, tmp_path, checks=checks)
    assert not gate["qualified"]
    (repo / "app.py").write_text("value = 2\n")
    assert not acceptance.validate_candidate(**kwargs)["passed"]


def test_noop_and_moved_head_fail_before_checks_or_closure(tmp_path):
    repo = _repo(tmp_path)
    kwargs, _ = _candidate(repo, tmp_path)
    assert acceptance.validate_candidate(**kwargs)["message"] == "candidate-no-change"
    (repo / "app.py").write_text("value = 2\n")
    _git(repo, "add", "app.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "unauthorized candidate commit")
    assert acceptance.validate_candidate(**kwargs)["message"] == "candidate-changed-head"


def test_retained_patch_contains_staged_and_unstaged_bytes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("value = 2\n")
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text("value = 3\n")
    path = acceptance.retain_candidate_patch(
        worktree_path=repo, output_dir=tmp_path / "state", identity="one",
    )
    assert b"+value = 3\n" in path.read_bytes()
    assert b"+value = 2\n" not in path.read_bytes()


@pytest.mark.parametrize("mutation", ["working-tree", "head", "index"])
def test_validation_cannot_change_the_checked_candidate(tmp_path, monkeypatch, mutation):
    from types import SimpleNamespace

    repo = _repo(tmp_path)
    kwargs, _ = _candidate(repo, tmp_path)
    (repo / "app.py").write_text("value = 2\n")

    def closure(*args, **options):
        if mutation == "working-tree":
            (repo / "app.py").write_text("value = 3\n")
        elif mutation == "index":
            _git(repo, "add", "app.py")
        else:
            _git(repo, "add", "app.py")
            _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "validator commit")
        return SimpleNamespace(is_closed=True)

    monkeypatch.setattr(acceptance, "verify_fix_closed", closure)
    verdict = acceptance.validate_candidate(**kwargs)
    assert not verdict["passed"]
    assert verdict["message"] == "candidate-changed-during-validation"


def test_retention_uses_git_patch_bytes_not_external_diff(tmp_path):
    repo = _repo(tmp_path)
    external = tmp_path / "external-diff"
    external.write_text("#!/bin/sh\nprintf 'not a patch\\n'\n")
    external.chmod(0o755)
    _git(repo, "config", "diff.external", str(external))
    (repo / "app.py").write_text("value = 2\n")
    signature = acceptance.candidate_signature(repo)
    path = acceptance.retain_candidate_patch(
        worktree_path=repo, output_dir=tmp_path / "state", identity="one",
        expected_signature=signature,
    )
    assert path.read_bytes().startswith(b"diff --git")
    assert b"+value = 2" in path.read_bytes()
    (repo / "app.py").write_text("value = 3\n")
    with pytest.raises(ValueError, match="candidate changed"):
        acceptance.retain_candidate_patch(
            worktree_path=repo, output_dir=tmp_path / "state", identity="one",
            expected_signature=signature,
        )
    assert len(list((tmp_path / "state").glob("*.patch"))) == 1


def test_untracked_candidate_cannot_be_silently_dropped(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("value = 2\n")
    (repo / "extra.py").write_text("unrepresented = True\n")
    with pytest.raises(ValueError, match="new files"):
        acceptance.retain_candidate_patch(worktree_path=repo, output_dir=tmp_path / "state", identity="one")
    assert not (tmp_path / "state").exists()
