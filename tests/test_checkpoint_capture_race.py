"""Deterministic post-scan swap tests for capture_checkpoint.

capture scans the tree (lstat sizes feed the byte ceiling), then creates its
staging directory via tempfile.mkdtemp, then copies file bytes. A monkeypatched
mkdtemp wrapper fires the swap exactly between scan and copy, so a swapped
symlink, replaced inode, grown file, or new hardlink lands in the real race
window: capture must refuse with CheckpointError, retain no partial backup,
and never modify the outside tree. The .git/index pre-check and the actual
bytes read during the copy are pinned the same way.
"""

import os
import tempfile
from pathlib import Path

import pytest

from bluei.engine.worktree_checkpoint import CheckpointError, capture_checkpoint
import bluei.engine.worktree_checkpoint as wt

OUTSIDE = b"outside-sentinel-bytes"


def _capture_refusing_staged_swap(monkeypatch, root, backup_root, swap, **kwargs):
    """Fire swap() at the mkdtemp staging dir: after the scan, before any copy."""
    fired = []
    real_mkdtemp = tempfile.mkdtemp

    def hooked_mkdtemp(*args, **mk_kwargs):
        path = real_mkdtemp(*args, **mk_kwargs)
        if not fired:
            fired.append(path)
            swap()
        return path

    monkeypatch.setattr(wt.tempfile, "mkdtemp", hooked_mkdtemp)
    with pytest.raises(CheckpointError) as excinfo:
        capture_checkpoint(root, backup_root, **kwargs)
    assert fired, "staging hook never fired"
    assert not isinstance(excinfo.value.__cause__, TypeError)
    assert not list(backup_root.iterdir()), "partial checkpoint retained after refusal"
    return excinfo


@pytest.mark.parametrize("case", ["file-symlink", "parent-symlink", "replaced-inode", "grown", "hardlinked"])
def test_capture_refuses_post_scan_swap(case, tmp_path, monkeypatch):
    root = tmp_path / "wt"
    sub = root / "sub"
    sub.mkdir(parents=True)
    target = sub / "file.txt"
    target.write_bytes(b"tiny")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_bytes(OUTSIDE)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    def file_symlink():
        staged = target.with_name("staged-link")
        os.symlink(outside / "file.txt", staged)
        os.replace(staged, target)

    def parent_symlink():
        os.rename(sub, tmp_path / "sub-moved")
        os.symlink(outside, sub)

    def replaced_inode():
        replacement = tmp_path / "replacement.bin"
        replacement.write_bytes(b"replaced-inode")
        os.replace(replacement, target)

    def grown():
        target.write_bytes(b"G" * 4096)

    def hardlinked():
        os.link(target, tmp_path / "second-link.bin")  # entry nlink becomes 2

    swap = {"file-symlink": file_symlink, "parent-symlink": parent_symlink,
            "replaced-inode": replaced_inode, "grown": grown, "hardlinked": hardlinked}[case]
    kwargs = {"max_bytes": 2048} if case == "grown" else {}

    _capture_refusing_staged_swap(monkeypatch, root, backup_root, swap, **kwargs)
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before


def test_capture_refuses_git_index_swapped_for_outside_symlink(tmp_path, monkeypatch):
    """The .git/index regular-file pre-check must not be separable from its read."""
    root = tmp_path / "wt"
    root.mkdir()
    (root / "file.txt").write_bytes(b"data")
    git = root / ".git"
    git.mkdir()
    index = git / "index"
    index.write_bytes(b"INDEX")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(OUTSIDE)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    fired = []
    real_open, real_path_open = os.open, Path.open

    def swap_index():
        staged = git / "index.swap"
        os.symlink(sentinel, staged)
        os.replace(staged, index)

    def hooked_open(path, flags, *args, **kwargs):
        if not fired and not flags & os.O_DIRECTORY and os.fspath(path) in (str(index), "index"):
            fired.append("os.open")
            swap_index()
        return real_open(path, flags, *args, **kwargs)

    def hooked_path_open(self, mode="r", *args, **kwargs):
        if not fired and self == index and "r" in mode and "b" in mode:
            fired.append("Path.open")
            swap_index()
        return real_path_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", hooked_open)
    monkeypatch.setattr(Path, "open", hooked_path_open)
    with pytest.raises(CheckpointError) as excinfo:
        capture_checkpoint(root, backup_root)
    assert fired, "index swap hook never fired"
    assert not isinstance(excinfo.value.__cause__, TypeError)
    assert not list(backup_root.iterdir())
    assert sentinel.read_bytes() == OUTSIDE


def test_capture_caps_bytes_actually_read_after_signature_check(tmp_path, monkeypatch):
    """A file passing the signature check must still be capped on bytes actually read."""
    root = tmp_path / "wt"
    root.mkdir()
    target = root / "target.bin"
    target.write_bytes(b"tiny")
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    identity = os.stat(target)[:2]
    real_fdopen = os.fdopen
    read_sizes = []

    class _GrowOnFirstRead:
        def __init__(self, stream):
            self._stream = stream
            self._grown = False

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def read(self, size=-1):
            read_sizes.append(size)
            if not self._grown:
                self._grown = True
                target.write_bytes(b"G" * 8192)  # same inode, grown after the fstat
            return self._stream.read(size)

        def __getattr__(self, name):
            return getattr(self._stream, name)

    def hooked_fdopen(fd, mode="r", *args, **kwargs):
        stream = real_fdopen(fd, mode, *args, **kwargs)
        if mode.startswith("rb") and os.fstat(stream.fileno())[:2] == identity:
            return _GrowOnFirstRead(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", hooked_fdopen)
    with pytest.raises(CheckpointError, match="byte ceiling"):
        capture_checkpoint(root, backup_root, max_bytes=64)
    assert read_sizes == [65]
    assert not list(backup_root.iterdir())


def test_checkpoint_roundtrip_through_symlinked_ancestor(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    root = alias / "wt"
    (root / ".git").mkdir(parents=True)
    (root / ".git/index").write_bytes(b"INDEX")
    target = root / "file"
    target.write_bytes(b"original")
    cp = capture_checkpoint(root, tmp_path / "backups")
    try:
        assert wt.changed_paths(cp) == set()
        target.write_bytes(b"modified")
        wt.restore_checkpoint(cp)
        assert target.read_bytes() == b"original"
        assert wt.changed_paths(cp) == set()
    finally:
        wt.discard_checkpoint(cp)


def test_capture_refuses_replaced_external_git_directory(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "index").write_bytes(b"original-index")
    (root / ".git").write_text(f"gitdir: {metadata}\n")
    backups = tmp_path / "backups"
    backups.mkdir()

    def replace_metadata():
        metadata.rename(tmp_path / "detached-metadata")
        metadata.mkdir()
        (metadata / "index").write_bytes(b"replacement-index")

    _capture_refusing_staged_swap(monkeypatch, root, backups, replace_metadata)
    assert (metadata / "index").read_bytes() == b"replacement-index"
    assert (tmp_path / "detached-metadata/index").read_bytes() == b"original-index"


def test_changed_paths_detects_replaced_external_git_directory(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "index").write_bytes(b"original-index")
    (root / ".git").write_text(f"gitdir: {metadata}\n")
    cp = capture_checkpoint(root, tmp_path / "backups")
    try:
        metadata.rename(tmp_path / "detached-metadata")
        metadata.mkdir()
        (metadata / "index").write_bytes(b"replacement-index")
        assert ".git" in wt.changed_paths(cp)
        assert (metadata / "index").read_bytes() == b"replacement-index"
        assert cp.index_backup.read_bytes() == b"original-index"
    finally:
        wt.discard_checkpoint(cp)


@pytest.mark.parametrize("case", ["empty-directory", "symlink"])
def test_capture_revalidates_non_file_entries(tmp_path, monkeypatch, case):
    root = tmp_path / "wt"
    root.mkdir()
    target = root / "entry"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(OUTSIDE)
    if case == "empty-directory":
        target.mkdir()
    else:
        target.symlink_to("original-target")
    backups = tmp_path / "backups"
    backups.mkdir()

    def swap_entry():
        if case == "empty-directory":
            target.rmdir()
            target.symlink_to(outside, target_is_directory=True)
        else:
            target.unlink()
            target.symlink_to("changed-target")

    _capture_refusing_staged_swap(monkeypatch, root, backups, swap_entry)
    assert (outside / "file").read_bytes() == OUTSIDE


@pytest.mark.parametrize("case", ["symlink", "oversized"])
def test_capture_refuses_raced_or_oversized_git_pointer(tmp_path, monkeypatch, case):
    root = tmp_path / "wt"
    root.mkdir()
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    pointer = root / ".git"
    pointer.write_text(f"gitdir: {metadata}\n")
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(OUTSIDE)
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    fired = []
    real_open = os.open

    def swap_then_open(path, flags, *args, **kwargs):
        if not fired and os.fspath(path) == ".git" and not flags & os.O_DIRECTORY:
            fired.append(path)
            if case == "symlink":
                pointer.unlink()
                pointer.symlink_to(sentinel)
            else:
                pointer.write_bytes(b"x" * 4097)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(CheckpointError) as excinfo:
        capture_checkpoint(root, backup_root)
    assert fired == [".git"]
    assert not isinstance(excinfo.value.__cause__, TypeError)
    assert sentinel.read_bytes() == OUTSIDE
    assert not list(backup_root.iterdir())
