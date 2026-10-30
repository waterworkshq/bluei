"""Deterministic symlink-race tests for anchored checkpoint restore.

A concurrent writer inside the worktree must never redirect restore
mutations outside the worktree: a parent swapped for an outside symlink
after the scan fails the restore (CheckpointRestoreError, backup
retained) without touching the outside sentinel.
"""

import contextlib
import errno
import os
import shutil
import stat
from pathlib import Path

import pytest

from bluei.engine import worktree_checkpoint as wt
from bluei.engine.worktree_checkpoint import (
    CheckpointRestoreError,
    capture_checkpoint,
    restore_checkpoint,
)


def _make_tree(root: Path) -> None:
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.txt").write_text("original")
    os.chmod(root / "sub" / "file.txt", 0o600)
    (root / "top.txt").write_text("top")
    os.chmod(root / "top.txt", 0o644)


def _sentinel(outside: Path) -> Path:
    outside.mkdir(parents=True, exist_ok=True)
    sentinel = outside / "sentinel"
    sentinel.write_text("do-not-touch")
    return sentinel


@contextlib.contextmanager
def _swap_parent_after_scan(monkeypatch, trigger_rel: str, root: Path, outside: Path):
    """Replace root/sub with an outside symlink at the first anchored open."""
    real = wt._open_parent
    fired = []

    @contextlib.contextmanager
    def spy(rootfd, rel):
        if rel == trigger_rel and not fired:
            fired.append(rel)
            shutil.rmtree(root / "sub")
            os.symlink(outside, root / "sub")
        with real(rootfd, rel) as target:
            yield target

    monkeypatch.setattr(wt, "_open_parent", spy)
    yield


def _assert_symlink_refusal(excinfo) -> None:
    """The failure must be the O_NOFOLLOW refusal, not an unrelated TypeError."""
    cause = excinfo.value.__cause__
    assert isinstance(cause, OSError)
    assert cause.errno in (errno.ELOOP, errno.ENOTDIR), cause


def test_restore_delete_refuses_parent_swapped_to_outside_symlink(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    _make_tree(root)
    outside = tmp_path / "outside"
    sentinel = _sentinel(outside)
    cp = capture_checkpoint(root, tmp_path / "backups")
    (root / "sub" / "file.txt").write_text("modified")  # forces delete + recreate

    with _swap_parent_after_scan(monkeypatch, "sub/file.txt", root, outside):
        with pytest.raises(CheckpointRestoreError) as excinfo:
            restore_checkpoint(cp)
    _assert_symlink_refusal(excinfo)

    assert sentinel.read_text() == "do-not-touch"
    assert os.listdir(outside) == ["sentinel"]
    assert cp.retained is True
    assert cp._backup_for("sub/file.txt").read_text() == "original"


def test_restore_create_never_writes_through_swapped_parent_symlink(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    _make_tree(root)
    outside = tmp_path / "outside"
    sentinel = _sentinel(outside)
    cp = capture_checkpoint(root, tmp_path / "backups")
    shutil.rmtree(root / "sub")  # file must be recreated from the backup

    with _swap_parent_after_scan(monkeypatch, "sub/file.txt", root, outside):
        with pytest.raises(CheckpointRestoreError) as excinfo:
            restore_checkpoint(cp)
    _assert_symlink_refusal(excinfo)

    assert sentinel.read_text() == "do-not-touch"
    assert "file.txt" not in os.listdir(outside)
    assert cp.retained is True
    assert cp._backup_for("sub/file.txt").read_text() == "original"


def test_restore_fails_stop_when_root_replaced_by_symlink(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    _make_tree(root)
    outside = tmp_path / "outside"
    sentinel = _sentinel(outside)
    cp = capture_checkpoint(root, tmp_path / "backups")
    (root / "sub" / "file.txt").write_text("modified")
    shutil.rmtree(root)
    os.symlink(outside, root)

    with pytest.raises(CheckpointRestoreError):
        restore_checkpoint(cp)

    assert sentinel.read_text() == "do-not-touch"
    assert os.listdir(outside) == ["sentinel"]
    assert cp.retained is True


def test_restore_fails_stop_when_root_directory_replaced(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    _make_tree(root)
    cp = capture_checkpoint(root, tmp_path / "backups")
    os.rename(root, tmp_path / "wt-kept")  # original inode stays occupied
    root.mkdir()  # guaranteed a different inode; Git-free, so metadata checks pass

    with pytest.raises(CheckpointRestoreError) as excinfo:
        restore_checkpoint(cp)

    assert "root identity changed" in str(excinfo.value)
    assert cp.retained is True


def test_restore_refuses_index_replaced_by_symlink(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "file.txt").write_text("data")
    git = root / ".git"
    git.mkdir()
    (git / "index").write_bytes(b"INDEX-DATA")
    cp = capture_checkpoint(root, tmp_path / "backups")
    assert cp.index_existed is True
    outside = tmp_path / "outside"
    sentinel = _sentinel(outside)
    (git / "index").unlink()
    os.symlink(sentinel, git / "index")

    with pytest.raises(CheckpointRestoreError) as excinfo:
        restore_checkpoint(cp)
    _assert_symlink_refusal(excinfo)

    assert sentinel.read_text() == "do-not-touch"
    assert cp.retained is True


def test_restore_refuses_index_hardlinked_to_outside_file(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "file.txt").write_text("data")
    git = root / ".git"
    git.mkdir()
    (git / "index").write_bytes(b"INDEX-DATA")
    cp = capture_checkpoint(root, tmp_path / "backups")
    assert cp.index_existed is True
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"sentinel-bytes")
    (git / "index").unlink()
    os.link(sentinel, git / "index")  # raced index sharing an outside inode

    with pytest.raises(CheckpointRestoreError):
        restore_checkpoint(cp)

    assert sentinel.read_bytes() == b"sentinel-bytes"  # never truncated
    assert cp.retained is True


def test_restore_noop_preserves_full_tree_and_modes(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    _make_tree(root)
    os.chmod(root / "sub", 0o707)
    os.symlink("sub/file.txt", root / "link")
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "deep" / "d.txt").write_text("deep")
    git = root / ".git"
    git.mkdir()
    (git / "index").write_bytes(b"INDEX-DATA")
    cp = capture_checkpoint(root, tmp_path / "backups")

    restore_checkpoint(cp)  # nothing changed: every entry must be kept

    assert (root / "sub" / "file.txt").read_text() == "original"
    assert stat.S_IMODE((root / "sub" / "file.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "sub").stat().st_mode) == 0o707
    assert stat.S_IMODE((root / "top.txt").stat().st_mode) == 0o644
    assert (root / "link").is_symlink()
    assert (root / "sub" / "deep" / "d.txt").read_text() == "deep"
    assert (git / "index").read_bytes() == b"INDEX-DATA"
    assert cp.retained is False


def test_restore_rebuilds_tree_with_anchored_operations(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    _make_tree(root)
    os.chmod(root / "sub", 0o707)
    os.symlink("sub/file.txt", root / "link")
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "deep" / "d.txt").write_text("deep")
    cp = capture_checkpoint(root, tmp_path / "backups")

    (root / "sub" / "file.txt").write_text("changed")
    (root / "junk").write_text("junk")
    (root / "link").unlink()
    os.chmod(root / "top.txt", 0o444)
    os.chmod(root / "sub", 0o755)  # snapshot dir present: mkdir EEXIST tolerance
    restore_checkpoint(cp)

    assert (root / "sub" / "file.txt").read_text() == "original"
    assert stat.S_IMODE((root / "sub" / "file.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "sub").stat().st_mode) == 0o707
    assert not (root / "junk").exists()
    assert (root / "link").is_symlink()
    assert os.readlink(root / "link") == "sub/file.txt"
    assert (root / "top.txt").read_text() == "top"
    assert stat.S_IMODE((root / "top.txt").stat().st_mode) == 0o644
    assert (root / "sub" / "deep" / "d.txt").read_text() == "deep"
    assert cp.retained is False
