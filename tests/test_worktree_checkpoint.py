"""Focused tests for the exact-content worktree checkpoint helper.

Covered: byte/mode/symlink/dir-topology exactness, git staged/untracked/
ignored state, index bytes and absent-index preservation, excluded log files,
pre-mutation ceiling rejection, backup placement rules, and the fail-stop
restore contract (retained backup, raising cleanup).
"""

import os
import stat
import subprocess

import pytest

from bluei.engine.worktree_checkpoint import (
    CheckpointError,
    CheckpointRestoreError,
    capture_checkpoint,
    changed_paths,
    discard_checkpoint,
    restore_checkpoint,
)


@pytest.fixture
def bk(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    return d


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)


def test_restore_exact_content_modes_symlinks_dirs(tmp_path, bk):
    wt = tmp_path / "wt"
    (wt / "src" / "deep").mkdir(parents=True)
    os.chmod(wt / "src", 0o751)
    f = wt / "src" / "deep" / "app.py"
    f.write_text("x = 1\n")
    os.chmod(f, 0o755)
    binary = wt / "data.bin"
    binary.write_bytes(bytes(range(256)))
    link = wt / "src" / "deep" / "link"
    link.symlink_to("../../data.bin")
    (wt / "empty").mkdir()
    gone = wt / "gone.py"
    gone.write_text("bye\n")

    cp = capture_checkpoint(wt, bk)
    assert changed_paths(cp) == set()

    # Mutate everything a stage could get wrong.
    f.write_text("CORRUPT\n")
    os.chmod(f, 0o600)
    os.chmod(wt / "src", 0o700)
    gone.unlink()
    binary.unlink()
    link.unlink()
    link.write_text("now a file\n")
    (wt / "empty").rmdir()
    (wt / "newdir").mkdir()
    (wt / "newdir" / "new.py").write_text("new\n")

    restore_checkpoint(cp)

    assert f.read_text() == "x = 1\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o755
    assert stat.S_IMODE((wt / "src").stat().st_mode) == 0o751
    assert gone.read_text() == "bye\n"
    assert binary.read_bytes() == bytes(range(256))
    assert link.is_symlink() and os.readlink(link) == "../../data.bin"
    assert (wt / "empty").is_dir()
    assert not (wt / "newdir").exists()
    assert changed_paths(cp) == set()
    discard_checkpoint(cp)
    assert not cp.backup_dir.exists()


def test_excluded_log_never_snapshotted_or_overwritten(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "code.py").write_text("a = 1\n")
    log = wt / "log.txt"
    log.write_text("line1\n")

    cp = capture_checkpoint(wt, bk, exclude_paths=[log])
    (wt / "code.py").write_text("MUTATED\n")
    log.write_text("line1\nline2\n")
    restore_checkpoint(cp)

    assert (wt / "code.py").read_text() == "a = 1\n"
    assert log.read_text() == "line1\nline2\n"


def test_git_staged_untracked_ignored_and_index_bytes(tmp_path, bk):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("v1\n")
    (repo / ".gitignore").write_text("ignored.txt\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    # Pre-attempt state: staged edit + unstaged edit + untracked + ignored.
    (repo / "deleted.py").write_text("d\n")
    _git(repo, "add", "deleted.py")
    _git(repo, "commit", "-m", "add deleted")
    (repo / "tracked.py").write_text("STAGED\n")
    _git(repo, "add", "tracked.py")
    (repo / "tracked.py").write_text("STAGED+UNSTAGED\n")
    (repo / "untracked.py").write_text("u\n")
    (repo / "ignored.txt").write_text("i\n")
    (repo / "deleted.py").unlink()  # pre-existing worktree deletion
    index_before = (repo / ".git" / "index").read_bytes()

    cp = capture_checkpoint(repo, bk)

    # Stage-like mutation: modify, delete, add, and git-add (index rewrite).
    (repo / "tracked.py").write_text("LINTER\n")
    (repo / "untracked.py").unlink()
    (repo / "ignored.txt").write_text("clobbered\n")
    (repo / "deleted.py").write_text("RESURRECTED\n")
    (repo / "extra.py").write_text("extra\n")
    _git(repo, "add", "tracked.py")
    assert (repo / ".git" / "index").read_bytes() != index_before

    restore_checkpoint(cp)

    assert (repo / "tracked.py").read_text() == "STAGED+UNSTAGED\n"
    assert (repo / "untracked.py").read_text() == "u\n"
    assert (repo / "ignored.txt").read_text() == "i\n"
    assert not (repo / "deleted.py").exists(), "pre-existing deletion restored"
    assert not (repo / "extra.py").exists()
    assert (repo / ".git" / "index").read_bytes() == index_before
    r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    )
    assert r.stdout.splitlines() == [
        " D deleted.py",
        "MM tracked.py",  # staged STAGED + unstaged STAGED+UNSTAGED
        "?? untracked.py",
    ], r.stdout


def test_git_linked_worktree_pointer_and_index(tmp_path, bk):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "m.py").write_text("m = 1\n")
    _git(main, "add", ".")
    _git(main, "commit", "-m", "init")
    wt = tmp_path / "linked"
    _git(main, "worktree", "add", str(wt), "-b", "branch-a")
    assert (wt / ".git").is_file()

    (wt / "m.py").write_text("STAGED\n")
    _git(wt, "add", "m.py")
    index_path = main / ".git" / "worktrees" / "linked" / "index"
    index_before = index_path.read_bytes()

    cp = capture_checkpoint(wt, bk)
    (wt / "m.py").write_text("MUT\n")
    _git(wt, "add", "m.py")
    restore_checkpoint(cp)

    assert (wt / "m.py").read_text() == "STAGED\n"
    assert (wt / ".git").is_file(), ".git pointer untouched"
    assert index_path.read_bytes() == index_before


def test_absent_git_index_stays_absent(tmp_path, bk):
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init")
    index = repo / ".git" / "index"
    if index.exists():
        pytest.skip("git init produced an index")
    cp = capture_checkpoint(repo, bk)
    (repo / "x.py").write_text("x\n")
    _git(repo, "add", "x.py")  # creates the index
    assert index.exists()
    restore_checkpoint(cp)
    assert not index.exists()
    assert not (repo / "x.py").exists()


def test_ceilings_reject_before_any_copy(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a").write_text("x")
    with pytest.raises(CheckpointError, match="file ceiling"):
        capture_checkpoint(wt, bk, max_files=0)
    with pytest.raises(CheckpointError, match="byte ceiling"):
        capture_checkpoint(wt, bk, max_bytes=0)
    assert list(bk.iterdir()) == [], "no backup dirs left behind on rejection"


def test_backup_root_inside_worktree_rejected(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a").write_text("x")
    with pytest.raises(CheckpointError, match="outside the worktree"):
        capture_checkpoint(wt, wt / "inside")


def test_special_files_rejected_before_copy(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    os.mkfifo(wt / "pipe")
    with pytest.raises(CheckpointError, match="special file"):
        capture_checkpoint(wt, bk)
    assert list(bk.iterdir()) == []


def test_failed_restore_retains_backup_and_discard_refuses(tmp_path, bk, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.py").write_text("original\n")
    cp = capture_checkpoint(wt, bk)
    (wt / "a.py").write_text("mutated\n")
    (wt / "b.py").write_text("extra\n")

    import bluei.engine.worktree_checkpoint as wcm

    real_copy = wcm.shutil.copyfile

    def failing_copy(src, dst, **kwargs):
        if str(dst).endswith("a.py") and str(dst).startswith(str(wt)):
            raise OSError("disk full")
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(wcm.shutil, "copyfile", failing_copy)
    with pytest.raises(CheckpointRestoreError, match="backup retained"):
        restore_checkpoint(cp)
    monkeypatch.undo()

    assert cp.retained is True
    assert cp.backup_dir.exists(), "recoverable evidence kept"
    discard_checkpoint(cp)  # must refuse to delete a retained backup
    assert cp.backup_dir.exists()


def test_symlink_parent_cannot_escape(tmp_path, bk):
    wt = tmp_path / "wt"
    (wt / "dir").mkdir(parents=True)
    (wt / "dir" / "file").write_text("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "file"
    sentinel.write_text("sentinel")
    cp = capture_checkpoint(wt, bk)
    (wt / "dir" / "file").unlink()
    (wt / "dir").rmdir()
    (wt / "dir").symlink_to(outside, target_is_directory=True)
    restore_checkpoint(cp)
    assert sentinel.read_text() == "sentinel"
    assert (wt / "dir" / "file").read_text() == "original"
    discard_checkpoint(cp)


def test_new_parent_with_excluded_log_is_preserved(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    log = wt / "new" / "deep" / "log"
    cp = capture_checkpoint(wt, bk, exclude_paths=[log])
    log.parent.mkdir(parents=True)
    log.write_text("telemetry")
    (log.parent / "source").write_text("new")
    restore_checkpoint(cp)
    assert log.read_text() == "telemetry"
    assert not (log.parent / "source").exists()
    discard_checkpoint(cp)


def test_root_and_empty_directory_modes(tmp_path, bk):
    wt = tmp_path / "wt"
    (wt / "empty").mkdir(parents=True)
    wt.chmod(0o751)
    (wt / "empty").chmod(0o711)
    cp = capture_checkpoint(wt, bk)
    wt.chmod(0o700)
    (wt / "empty").chmod(0o700)
    assert "." in changed_paths(cp)
    restore_checkpoint(cp)
    assert stat.S_IMODE(wt.stat().st_mode) == 0o751
    assert stat.S_IMODE((wt / "empty").stat().st_mode) == 0o711
    discard_checkpoint(cp)


def test_git_index_change_reported_and_backup_names_do_not_collide(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init")
    (wt / "git_index.bin").write_bytes(b"source")
    _git(wt, "add", "git_index.bin")
    cp = capture_checkpoint(wt, bk)
    (wt / "git_index.bin").write_bytes(b"changed")
    _git(wt, "add", "git_index.bin")
    assert ".git/index" in changed_paths(cp)
    restore_checkpoint(cp)
    assert (wt / "git_index.bin").read_bytes() == b"source"
    discard_checkpoint(cp)


def test_changed_git_pointer_fails_stop(tmp_path, bk):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "file").write_text("original")
    _git(main, "add", "file")
    _git(main, "commit", "-m", "init")
    wt = tmp_path / "linked"
    _git(main, "worktree", "add", str(wt), "-b", "test-pointer")
    cp = capture_checkpoint(wt, bk)
    pointer = wt / ".git"
    pointer.write_bytes(pointer.read_bytes() + b"\n")
    assert ".git" in changed_paths(cp)
    with pytest.raises(CheckpointRestoreError, match=str(cp.backup_dir)):
        restore_checkpoint(cp)
    assert cp.retained and pointer.is_file()


def test_nested_git_metadata_fails_before_deletion(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    cp = capture_checkpoint(wt, bk)
    nested = wt / "new"
    nested.mkdir()
    _git(nested, "init")
    with pytest.raises(CheckpointRestoreError, match="nested Git metadata"):
        restore_checkpoint(cp)
    assert (nested / ".git").is_dir() and cp.retained
    with pytest.raises(CheckpointError, match="nested Git metadata"):
        capture_checkpoint(wt, bk)


def test_capture_failures_are_normalized(tmp_path, bk, monkeypatch):
    import bluei.engine.worktree_checkpoint as wcm
    with pytest.raises(CheckpointError):
        capture_checkpoint(tmp_path / "missing", bk)
    wt = tmp_path / "wt"
    wt.mkdir()
    link = tmp_path / "link"
    link.symlink_to(wt, target_is_directory=True)
    with pytest.raises(CheckpointError):
        capture_checkpoint(link, bk)
    (wt / ".git").write_text("not a git pointer")
    with pytest.raises(CheckpointError):
        capture_checkpoint(wt, bk)
    (wt / ".git").unlink()
    monkeypatch.setattr(wcm, "_walk", lambda *args: (_ for _ in ()).throw(OSError("walk failed")))
    with pytest.raises(CheckpointError, match="walk failed"):
        capture_checkpoint(wt, bk)
    monkeypatch.undo()
    blocked = tmp_path / "file"
    blocked.write_text("x")
    with pytest.raises(CheckpointError):
        capture_checkpoint(wt, blocked / "backups")


def test_created_git_metadata_fails_stop(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    cp = capture_checkpoint(wt, bk)
    _git(wt, "init")
    assert ".git" in changed_paths(cp)
    with pytest.raises(CheckpointRestoreError, match="Git metadata changed"):
        restore_checkpoint(cp)
    assert (wt / ".git").is_dir() and cp.retained


def test_unreadable_git_pointer_is_not_skipped(tmp_path, bk, monkeypatch):
    from pathlib import Path
    wt = tmp_path / "wt"
    wt.mkdir()
    pointer = wt / ".git"
    pointer.write_text("gitdir: missing")
    original = Path.read_bytes
    def denied(path):
        if path == pointer:
            raise PermissionError("denied")
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(CheckpointError, match="denied"):
        capture_checkpoint(wt, bk)


def test_new_nested_git_in_excluded_directory_fails_stop(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    excluded = wt / "logs"
    cp = capture_checkpoint(wt, bk, exclude_paths=[excluded])
    excluded.mkdir()
    _git(excluded, "init")
    with pytest.raises(CheckpointRestoreError, match="nested Git metadata"):
        restore_checkpoint(cp)
    assert (excluded / ".git").is_dir() and cp.retained


def test_read_only_root_restore_retains_backup(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "file").write_text("original")
    cp = capture_checkpoint(wt, bk)
    (wt / "file").write_text("changed")
    wt.chmod(0o555)
    try:
        if os.access(wt, os.W_OK):
            pytest.skip("current user bypasses directory permissions")
        with pytest.raises(CheckpointRestoreError, match=str(cp.backup_dir)):
            restore_checkpoint(cp)
        assert cp.retained and cp.backup_dir.exists()
        assert (wt / "file").read_text() == "changed"
    finally:
        wt.chmod(0o755)


@pytest.mark.parametrize("alias_kind", ["file", "ancestor", "external"])
def test_symlinked_exclusion_rejected(tmp_path, bk, alias_kind):
    wt = tmp_path / "wt"
    (wt / "logs").mkdir(parents=True)
    log = wt / "logs" / "output"
    log.write_text("before")
    if alias_kind == "file":
        alias = wt / "alias"
        alias.symlink_to(log)
    elif alias_kind == "ancestor":
        (wt / "alias").symlink_to(wt / "logs", target_is_directory=True)
        alias = wt / "alias" / "output"
    else:
        (tmp_path / "alias").symlink_to(wt, target_is_directory=True)
        alias = tmp_path / "alias" / "logs" / "output"
    with pytest.raises(CheckpointError, match="exclusion"):
        capture_checkpoint(wt, bk, exclude_paths=[alias])
    assert log.read_text() == "before"
    assert list(bk.iterdir()) == []


def test_hardlinked_files_rejected_without_changes(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    source = wt / "source"
    source.write_bytes(b"original")
    alias = tmp_path / "external"
    os.link(source, alias)
    inode = source.stat().st_ino
    with pytest.raises(CheckpointError, match="hardlinked"):
        capture_checkpoint(wt, bk)
    assert source.stat().st_ino == alias.stat().st_ino == inode
    assert source.read_bytes() == alias.read_bytes() == b"original"
    assert list(bk.iterdir()) == []


def test_restore_special_file_fails_without_deleting_it(tmp_path, bk):
    wt = tmp_path / "wt"
    wt.mkdir()
    cp = capture_checkpoint(wt, bk)
    os.mkfifo(wt / "pipe")
    with pytest.raises(CheckpointRestoreError, match="special file"):
        restore_checkpoint(cp)
    assert stat.S_ISFIFO((wt / "pipe").lstat().st_mode) and cp.retained
