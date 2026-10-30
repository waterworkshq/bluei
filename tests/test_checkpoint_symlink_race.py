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
import subprocess
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

    with pytest.raises(CheckpointRestoreError, match="unsupported Git index"):
        restore_checkpoint(cp)

    assert sentinel.read_text() == "do-not-touch"
    assert cp.retained is True


def test_restore_replaces_hardlinked_index_without_touching_outside_file(tmp_path):
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
    os.link(sentinel, git / "index")  # index sharing an outside inode
    mode = stat.S_IMODE(sentinel.stat().st_mode)

    restore_checkpoint(cp)

    assert sentinel.read_bytes() == b"sentinel-bytes"  # outside link keeps its bytes
    assert sentinel.lstat().st_nlink == 1  # old inode unlinked from the index only
    assert (git / "index").read_bytes() == b"INDEX-DATA"  # fresh inode, backup bytes
    assert stat.S_IMODE((git / "index").stat().st_mode) == mode
    assert os.listdir(git) == ["index"]  # no temp residue
    assert cp.retained is False


def _git_index(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "tracked.txt").write_text("v1\n")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    return root / ".git" / "index"


def test_index_hardlink_race_during_restore_leaves_outside_inode_untouched(tmp_path, monkeypatch):
    """A hardlink created after any pre-check must not let restore bytes escape.

    The hook fires inside _fd_copy, the last pre-write step both the old and
    new implementations share. Old code had already passed its nlink check and
    ftruncate'd the live inode, so the link lands in the race window and the
    copy corrupts the outside file (sentinel bytes != raced). New code copies
    onto a fresh temp inode, so the linked inode keeps its own bytes.
    """
    root = tmp_path / "wt"
    git_index = _git_index(root)
    cp = capture_checkpoint(root, tmp_path / "backups")
    checkpointed = git_index.read_bytes()
    (root / "tracked.txt").write_text("v2\n")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    raced = git_index.read_bytes()  # a real Git process rewrote the index
    assert raced != checkpointed

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    real_copy = wt._fd_copy
    fired = []

    def hardlink_before_index_copy(src, fd, mode=None):
        if Path(src) == cp.index_backup and not fired:
            fired.append(True)
            os.link(git_index, sentinel)  # attacker links after every check
            linked = os.stat(sentinel)
            live = os.stat(git_index)
            assert (linked.st_dev, linked.st_ino) == (live.st_dev, live.st_ino)
        return real_copy(src, fd, mode)

    monkeypatch.setattr(wt, "_fd_copy", hardlink_before_index_copy)
    restore_checkpoint(cp)

    assert fired  # the race was actually staged, not skipped by an error
    assert sentinel.read_bytes() == raced  # outside inode never overwritten
    assert git_index.read_bytes() == checkpointed  # checkpointed index restored
    assert sentinel.lstat().st_nlink == 1
    assert not [n for n in os.listdir(root / ".git") if "bluei-restore" in n]
    wt.discard_checkpoint(cp)


def test_index_restore_failure_cleans_temp_and_retains_backup(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "f.txt").write_text("data")
    git = root / ".git"
    git.mkdir()
    (git / "index").write_bytes(b"INDEX-DATA")
    cp = capture_checkpoint(root, tmp_path / "backups")
    (git / "index").write_bytes(b"MUTATED")

    real_replace = os.replace

    def fail_index_replace(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        if dst_dir_fd is not None:
            raise OSError(errno.EIO, "simulated index publish failure")
        return real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", fail_index_replace)
    with pytest.raises(CheckpointRestoreError, match="checkpoint restore failed"):
        restore_checkpoint(cp)

    assert os.listdir(git) == ["index"]  # temp cleaned up
    assert (git / "index").read_bytes() == b"MUTATED"  # old inode untouched
    assert cp.retained is True
    assert cp.git_dir_fd is None  # recovery descriptor closed
    wt.discard_checkpoint(cp)
    assert cp.backup_dir.exists()  # retained for manual recovery


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


def test_index_restore_stays_anchored_when_root_path_is_replaced(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    (root / ".git").mkdir(parents=True)
    (root / ".git/index").write_bytes(b"original-index")
    (root / "source").write_text("original")
    cp = capture_checkpoint(root, tmp_path / "backups")
    (root / "source").write_text("changed")
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    sentinel = outside / ".git/index"
    sentinel.write_bytes(b"outside-index")
    old = tmp_path / "original-root"
    real_copy = wt._fd_copy
    swapped = []
    def replace_root_then_copy(src, fd, mode=None):
        if not swapped:
            swapped.append(True)
            root.rename(old)
            root.symlink_to(outside, target_is_directory=True)
        return real_copy(src, fd, mode)
    monkeypatch.setattr(wt, "_fd_copy", replace_root_then_copy)
    try:
        restore_checkpoint(cp)
    except CheckpointRestoreError:
        assert cp.retained
    assert swapped
    assert sentinel.read_bytes() == b"outside-index"
    assert (old / ".git/index").read_bytes() == b"original-index"
    wt.discard_checkpoint(cp)


def test_small_file_special_mode_is_applied_after_buffer_flush(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("mode-clearing behavior requires a non-root writer")
    root = tmp_path / "wt"
    root.mkdir()
    target = root / "program"
    target.write_bytes(b"small original content")
    target.chmod(0o4755)
    cp = capture_checkpoint(root, tmp_path / "backups")
    target.write_bytes(b"changed")
    restore_checkpoint(cp)
    assert target.read_bytes() == b"small original content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o4755
    wt.discard_checkpoint(cp)


def test_comparison_rechecks_file_kind_after_scan(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    target = root / "file"
    target.write_text("original")
    outside = tmp_path / "outside"
    outside.write_text("original")
    cp = capture_checkpoint(root, tmp_path / "backups")
    real_match = wt._matches_snapshot
    fired = []
    def swap_before_comparison(snapshot, rel, entry, path):
        if rel == "file" and not fired:
            fired.append(True)
            target.unlink()
            target.symlink_to(outside)
        return real_match(snapshot, rel, entry, path)
    monkeypatch.setattr(wt, "_matches_snapshot", swap_before_comparison)
    restore_checkpoint(cp)
    assert fired and not target.is_symlink()
    assert target.read_text() == outside.read_text() == "original"
    wt.discard_checkpoint(cp)


def test_replaced_external_git_directory_fails_and_closes_descriptor(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "index").write_bytes(b"original-index")
    (root / ".git").write_text(f"gitdir: {metadata}\n")
    cp = capture_checkpoint(root, tmp_path / "backups")
    descriptor = cp.git_dir_fd
    metadata.rename(tmp_path / "old-metadata")
    metadata.mkdir()
    (metadata / "index").write_bytes(b"replacement-index")
    with pytest.raises(CheckpointRestoreError, match="external Git directory identity changed"):
        restore_checkpoint(cp)
    assert (metadata / "index").read_bytes() == b"replacement-index"
    assert cp.retained and cp.backup_dir.exists() and cp.git_dir_fd is None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    wt.discard_checkpoint(cp)
    assert cp.backup_dir.exists()
