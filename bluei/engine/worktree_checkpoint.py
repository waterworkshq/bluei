"""Bounded, byte-exact worktree and Git index checkpoints.

ponytail: Full copies are capped at 50,000 entries and 256 MiB; use a
content-addressed snapshot if larger worktrees need support.
"""

import contextlib
import errno
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

MAX_FILES = 50_000
MAX_BYTES = 256 * 1024 * 1024


class CheckpointError(Exception):
    """The worktree cannot be safely checkpointed."""


class CheckpointRestoreError(Exception):
    """Restore failed; the backup is retained for recovery."""


@dataclass
class _Entry:
    kind: str
    mode: int = 0
    target: str = ""
    source_stat: Optional[os.stat_result] = field(default=None, compare=False, repr=False)


@dataclass
class WorktreeCheckpoint:
    root: Path
    backup_dir: Path
    entries: dict[str, _Entry] = field(default_factory=dict)
    exclude_paths: set[str] = field(default_factory=set)
    index_backup: Optional[Path] = None
    index_target: Optional[Path] = None
    index_existed: bool = False
    retained: bool = False
    root_mode: int = 0
    root_identity: object = None
    git_identity: object = None
    git_dir_fd: Optional[int] = None

    def _backup_for(self, rel: str) -> Path:
        return self.backup_dir / "data" / rel


def _raise_walk_error(err: OSError) -> None:
    raise err


def _walk(root: Path, exclude: set[str]):
    """Walk without following links; nested Git metadata is never disposable."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False, onerror=_raise_walk_error):
        d = Path(dirpath)
        if d != root and ".git" in dirnames + filenames:
            raise CheckpointError(f"nested Git metadata at {d / '.git'}")
        if d != root:
            st = d.lstat()
            if not stat.S_ISDIR(st.st_mode):
                raise CheckpointError(f"directory changed during scan: {d}")
            yield str(d.relative_to(root)), d, _Entry("dir", stat.S_IMODE(st.st_mode), source_stat=st)
        for name in dirnames[:]:
            p = d / name
            if d == root and name == ".git":
                dirnames.remove(name)
            elif str(p) in exclude:
                dirnames.remove(name)
                if not p.is_symlink():
                    for nested, dirs, files in os.walk(p, followlinks=False, onerror=_raise_walk_error):
                        if ".git" in dirs + files:
                            raise CheckpointError(f"nested Git metadata at {Path(nested) / '.git'}")
            elif p.is_symlink():
                dirnames.remove(name)
                st = p.lstat()
                if not stat.S_ISLNK(st.st_mode):
                    raise CheckpointError(f"symlink changed during scan: {p}")
                yield str(p.relative_to(root)), p, _Entry("symlink", target=os.readlink(p), source_stat=st)
        for name in filenames:
            p = d / name
            if (d == root and name == ".git") or str(p) in exclude:
                continue
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entry = _Entry("symlink", target=os.readlink(p), source_stat=st)
            elif stat.S_ISREG(st.st_mode):
                entry = _Entry("file", stat.S_IMODE(st.st_mode), source_stat=st)
            else:
                raise CheckpointError(f"unsupported special file in worktree: {p}")
            yield str(p.relative_to(root)), p, entry


def _git_metadata(root: Path, *, rootfd: Optional[int] = None):
    """Record the root .git identity and resolve a verified index location."""
    if rootfd is None:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            return _git_metadata(root, rootfd=fd)
        finally:
            os.close(fd)
    link = root / ".git"
    try:
        st = os.stat(".git", dir_fd=rootfd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    identity = (st.st_dev, st.st_ino, st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        gitdir = link
    elif stat.S_ISREG(st.st_mode):
        fd = os.open(".git", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=rootfd)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_signature(opened) != _file_signature(st):
                raise CheckpointError(f"Git pointer changed during capture: {link}")
            # ponytail: Git pointers over 4 KiB are unsupported; widen only with a bounded format.
            data = stream.read(4097)
            if len(data) > 4096 or _file_signature(os.fstat(stream.fileno())) != _file_signature(opened):
                raise CheckpointError(f"invalid or changed .git pointer at {link}")
        try:
            text = data.decode("utf-8").strip()
        except UnicodeError as exc:
            raise CheckpointError(f"invalid .git pointer at {link}") from exc
        if not text.startswith("gitdir: ") or not text[8:].strip() or "\n" in text:
            raise CheckpointError(f"invalid .git pointer at {link}")
        gitdir = Path(text[8:].strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        gitdir = gitdir.resolve(strict=True)
        identity += (data,)
    else:
        raise CheckpointError(f"unsupported .git metadata at {link}")
    if not gitdir.is_dir():
        raise CheckpointError(f"invalid Git directory: {gitdir}")
    if gitdir != link and gitdir.is_relative_to(root.resolve()):
        raise CheckpointError(f"Git directory inside worktree: {gitdir}")
    return identity, gitdir / "index"


def _file_signature(st: os.stat_result):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _index_bytes(path: Optional[Path], max_bytes: int = MAX_BYTES, *, dir_fd: Optional[int]):
    if path is None:
        return None
    if dir_fd is None:
        raise CheckpointError("Git directory descriptor missing")
    try:
        st = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise CheckpointError(f"unsupported Git index: {path}")
    if st.st_size > max_bytes:
        raise CheckpointError(f"checkpoint over byte ceiling ({max_bytes})")
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _file_signature(opened) != _file_signature(st):
            raise CheckpointError(f"Git index changed during capture: {path}")
        data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise CheckpointError(f"checkpoint over byte ceiling ({max_bytes})")
        if _file_signature(os.fstat(stream.fileno())) != _file_signature(opened):
            raise CheckpointError(f"Git index changed during capture: {path}")
    return data


def _root_mode(root: Path):
    st = root.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise CheckpointError(f"worktree root is not a real directory: {root}")
    return stat.S_IMODE(st.st_mode)


def capture_checkpoint(root: Path, backup_root: Path, *, exclude_paths: Iterable[Path] = (),
                       max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> WorktreeCheckpoint:
    backup_dir = None
    rootfd = gitfd = None
    try:
        root = Path(os.path.abspath(root))
        root_mode = _root_mode(root)
        root_st = root.lstat()
        root_identity = (root_st.st_dev, root_st.st_ino)
        rootfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened_root = os.fstat(rootfd)
        if (opened_root.st_dev, opened_root.st_ino) != root_identity:
            raise CheckpointError("worktree root identity changed during capture")
        exclude = {os.path.abspath(p) for p in exclude_paths}
        for item in exclude:
            path = Path(item)
            resolved = path.resolve()
            if path.is_relative_to(root):
                if resolved != root.resolve() / path.relative_to(root):
                    raise CheckpointError(f"symlinked exclusion: {path}")
            elif resolved.is_relative_to(root.resolve()):
                raise CheckpointError(f"external exclusion aliases worktree: {path}")
        backup_root = Path(backup_root).resolve()
        if backup_root == root or root in backup_root.parents or backup_root == root.resolve() or root.resolve() in backup_root.parents:
            raise CheckpointError(f"backup root {backup_root} must live outside the worktree {root}")
        identity, index = _git_metadata(root, rootfd=rootfd)
        if index is not None:
            if index.parent == root / ".git":
                gitfd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
                opened_git = os.fstat(gitfd)
                if (opened_git.st_dev, opened_git.st_ino, opened_git.st_mode) != identity:
                    raise CheckpointError("Git directory identity changed during capture")
            else:
                anchor = os.open(index.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    with _open_parent(anchor, str(index.relative_to(index.anchor))) as (parentfd, _):
                        gitfd = os.dup(parentfd)
                finally:
                    os.close(anchor)
        index_data = _index_bytes(index, max_bytes, dir_fd=gitfd)
        entries = {}
        total = len(index_data) if index_data is not None else 0
        for rel, path, entry in _walk(root, exclude):
            entries[rel] = entry
            if len(entries) > max_files:
                raise CheckpointError(f"checkpoint over file ceiling ({max_files})")
            if entry.kind == "file":
                st = entry.source_stat
                # ponytail: hardlinks are unsupported; preserve inode groups in a future snapshot format.
                if st.st_nlink > 1:
                    raise CheckpointError(f"unsupported hardlinked file: {path}")
                total += st.st_size
                if total > max_bytes:
                    raise CheckpointError(f"checkpoint over byte ceiling ({max_bytes})")
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix="bluei-ckpt-", dir=backup_root))
        cp = WorktreeCheckpoint(root, backup_dir, entries, exclude, root_mode=root_mode,
                                root_identity=root_identity,
                                git_identity=identity, index_target=index, index_existed=index_data is not None)
        total = len(index_data) if index_data is not None else 0
        for rel, entry in entries.items():
            if entry.kind == "file":
                dest = cp._backup_for(rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with _open_parent(rootfd, rel) as (parentfd, name):
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parentfd)
                with os.fdopen(fd, "rb") as source, dest.open("xb") as target:
                    opened = os.fstat(source.fileno())
                    if not stat.S_ISREG(opened.st_mode) or _file_signature(opened) != _file_signature(entry.source_stat):
                        raise CheckpointError(f"source file changed during capture: {rel}")
                    while True:
                        block = source.read(min(1024 * 1024, max_bytes - total + 1))
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise CheckpointError(f"checkpoint over byte ceiling ({max_bytes})")
                        target.write(block)
                    if _file_signature(os.fstat(source.fileno())) != _file_signature(opened):
                        raise CheckpointError(f"source file changed during capture: {rel}")
        for rel, entry in entries.items():
            if entry.kind == "file":
                continue
            with _open_parent(rootfd, rel) as (parentfd, name):
                current = os.stat(name, dir_fd=parentfd, follow_symlinks=False)
                if _file_signature(current) != _file_signature(entry.source_stat):
                    raise CheckpointError(f"source entry changed during capture: {rel}")
                if entry.kind == "symlink" and os.readlink(name, dir_fd=parentfd) != entry.target:
                    raise CheckpointError(f"symlink changed during capture: {rel}")
        if index_data is not None:
            cp.index_backup = backup_dir / "git_index.bin"
            cp.index_backup.write_bytes(index_data)
        if _git_metadata(root, rootfd=rootfd) != (identity, index):
            raise CheckpointError("root Git metadata changed during capture")
        if index is not None:
            active_git = index.parent.stat(follow_symlinks=False)
            captured_git = os.fstat(gitfd)
            if (active_git.st_dev, active_git.st_ino, active_git.st_mode) != (captured_git.st_dev, captured_git.st_ino, captured_git.st_mode):
                raise CheckpointError("Git directory identity changed during capture")
        current_root = root.lstat()
        if (current_root.st_dev, current_root.st_ino) != root_identity:
            raise CheckpointError("worktree root identity changed during capture")
        cp.git_dir_fd, gitfd = gitfd, None
        return cp
    except BaseException as exc:
        if backup_dir is not None:
            try:
                shutil.rmtree(backup_dir)
            except OSError as cleanup:
                raise CheckpointError(f"checkpoint capture failed: {exc}; partial backup retained at {backup_dir}: {cleanup}") from exc
        if isinstance(exc, (OSError, CheckpointError)):
            raise CheckpointError(f"checkpoint capture failed: {exc}") from exc
        raise
    finally:
        for fd in (rootfd, gitfd):
            if fd is not None:
                os.close(fd)


def _matches_snapshot(cp: WorktreeCheckpoint, rel: str, entry: _Entry, path: Path) -> bool:
    old = cp.entries.get(rel)
    if old is None or old.kind != entry.kind:
        return False
    current_stat = path.lstat()
    if entry.kind == "symlink":
        return stat.S_ISLNK(current_stat.st_mode) and old.target == os.readlink(path)
    if entry.kind == "dir":
        return stat.S_ISDIR(current_stat.st_mode) and old.mode == stat.S_IMODE(current_stat.st_mode)
    if not stat.S_ISREG(current_stat.st_mode) or old.mode != stat.S_IMODE(current_stat.st_mode):
        return False
    backup = cp._backup_for(rel)
    if current_stat.st_size != backup.stat().st_size:
        return False
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOENT):
            return False
        raise
    with os.fdopen(fd, "rb") as current, backup.open("rb") as original:
        opened = os.fstat(current.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (current_stat.st_dev, current_stat.st_ino):
            return False
        while True:
            block = current.read(1024 * 1024)
            if block != original.read(1024 * 1024):
                return False
            if not block:
                return True


@contextlib.contextmanager
def _open_parent(rootfd: int, rel: str):
    """Anchor rel's parent directory, refusing swapped symlink components."""
    parts = Path(rel).parts
    fd = rootfd
    chain = []
    try:
        for comp in parts[:-1]:
            fd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            chain.append(fd)
        yield fd, parts[-1]
    finally:
        for opened in chain:
            os.close(opened)


def _fd_copy(src: Path, dst_fd: int, mode: Optional[int] = None) -> None:
    """Copy trusted backup bytes onto an already-anchored destination fd.

    Takes ownership of dst_fd; entering fdopen first guarantees the fd is
    closed even when opening src or the copy fails.
    """
    with os.fdopen(dst_fd, "wb") as target, open(src, "rb") as source:
        shutil.copyfileobj(source, target)
        if mode is not None:
            target.flush()
            os.fchmod(target.fileno(), mode)


def restore_checkpoint(cp: WorktreeCheckpoint) -> None:
    """Restore without traversing replacement symlinks or deleting excluded data.

    ponytail: the scan may still race a concurrent writer (reads only); every
    mutation is anchored to a root fd opened with O_NOFOLLOW, so a parent
    swapped for an outside symlink fails closed instead of escaping the tree.
    """
    try:
        _root_mode(cp.root)
        identity, index = _git_metadata(cp.root)
        if identity != cp.git_identity or index != cp.index_target:
            raise CheckpointError("root Git metadata changed")
        rootfd = os.open(cp.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root_st = os.fstat(rootfd)
            if cp.root_identity is not None and (root_st.st_dev, root_st.st_ino) != cp.root_identity:
                raise CheckpointError("worktree root identity changed")
            # Scan before modifying anything: special files and nested repos fail-stop.
            scanned = list(_walk(cp.root, cp.exclude_paths))
            current = {rel: entry for rel, _, entry in scanned}
            # Unchanged entries (same kind, mode, bytes/link target) are left untouched.
            kept = {rel for rel, p, entry in scanned if _matches_snapshot(cp, rel, entry, p)}
            # Delete bottom-up, never recursively: excluded children prevent rmdir.
            for rel in sorted(current, key=lambda r: len(Path(r).parts), reverse=True):
                if rel in kept:
                    continue
                entry = current[rel]
                with _open_parent(rootfd, rel) as (parentfd, name):
                    if entry.kind == "dir":
                        previous = cp.entries.get(rel)
                        if previous is not None and previous.kind == "dir":
                            continue
                        try:
                            os.rmdir(name, dir_fd=parentfd)
                        except OSError as exc:
                            # A new directory containing excluded paths must remain.
                            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) and cp.entries.get(rel) is None and any(Path(x).is_relative_to(cp.root / rel) for x in cp.exclude_paths):
                                continue
                            raise
                    else:
                        os.unlink(name, dir_fd=parentfd)
            for rel, entry in sorted(cp.entries.items(), key=lambda item: len(Path(item[0]).parts)):
                if rel in kept:
                    continue
                with _open_parent(rootfd, rel) as (parentfd, name):
                    if entry.kind == "dir":
                        try:
                            os.mkdir(name, 0o755, dir_fd=parentfd)
                        except FileExistsError:
                            st = os.stat(name, dir_fd=parentfd, follow_symlinks=False)
                            if not stat.S_ISDIR(st.st_mode):
                                raise
                    elif entry.kind == "symlink":
                        os.symlink(entry.target, name, dir_fd=parentfd)
                    else:
                        # O_EXCL|O_NOFOLLOW: never create through or over a raced name.
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parentfd)
                        _fd_copy(cp._backup_for(rel), fd, entry.mode)
            for rel, entry in sorted(cp.entries.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
                if entry.kind == "dir" and rel not in kept:
                    with _open_parent(rootfd, rel) as (parentfd, name):
                        dirfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parentfd)
                        try:
                            os.fchmod(dirfd, entry.mode)
                        finally:
                            os.close(dirfd)
            os.fchmod(rootfd, cp.root_mode)
            if index is not None:
                if cp.git_dir_fd is None:
                    raise CheckpointError("Git directory descriptor missing")
                captured = os.fstat(cp.git_dir_fd)
                if index.parent == cp.root / ".git":
                    gitfd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
                else:
                    active_git = index.parent.stat()
                    if (active_git.st_dev, active_git.st_ino) != (captured.st_dev, captured.st_ino):
                        raise CheckpointError("external Git directory identity changed")
                    gitfd = os.dup(cp.git_dir_fd)
                try:
                    current_git = os.fstat(gitfd)
                    if (current_git.st_dev, current_git.st_ino) != (captured.st_dev, captured.st_ino):
                        raise CheckpointError("Git directory identity changed")
                    if cp.index_existed:
                        # Writing the live inode in place (fstat then ftruncate)
                        # races a hardlink created after the check, corrupting
                        # bytes outside the worktree. Publish a fresh exclusive
                        # inode instead: replace never mutates the old one, so
                        # outside links keep their original bytes.
                        try:
                            st = os.stat(index.name, dir_fd=gitfd, follow_symlinks=False)
                        except FileNotFoundError:
                            st = None
                        if st is not None and not stat.S_ISREG(st.st_mode):
                            raise CheckpointError(f"unsupported Git index: {index}")
                        mode = stat.S_IMODE(st.st_mode) if st is not None else 0o600
                        tmp = f"index.bluei-restore-{os.getpid()}.tmp"
                        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=gitfd)
                        try:
                            _fd_copy(cp.index_backup, fd, mode)
                            os.replace(tmp, index.name, src_dir_fd=gitfd, dst_dir_fd=gitfd)
                        except BaseException:
                            with contextlib.suppress(OSError):
                                os.unlink(tmp, dir_fd=gitfd)
                            raise
                    else:
                        try:
                            os.unlink(index.name, dir_fd=gitfd)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(gitfd)
        finally:
            os.close(rootfd)
    except BaseException as exc:
        cp.retained = True
        if cp.git_dir_fd is not None:
            os.close(cp.git_dir_fd)
            cp.git_dir_fd = None
        raise CheckpointRestoreError(f"checkpoint restore failed: {exc}; backup retained at {cp.backup_dir}") from exc


def discard_checkpoint(cp: WorktreeCheckpoint) -> None:
    if cp.git_dir_fd is not None:
        os.close(cp.git_dir_fd)
        cp.git_dir_fd = None
    if not cp.retained:
        shutil.rmtree(cp.backup_dir)


def changed_paths(cp: WorktreeCheckpoint) -> set[str]:
    changed = set()
    if _root_mode(cp.root) != cp.root_mode:
        changed.add(".")
    identity, index = _git_metadata(cp.root)
    if identity != cp.git_identity or index != cp.index_target:
        changed.add(".git")
    elif index is not None:
        if cp.git_dir_fd is None:
            raise CheckpointError("Git directory descriptor missing")
        active_git = index.parent.stat(follow_symlinks=False)
        captured_git = os.fstat(cp.git_dir_fd)
        if (active_git.st_dev, active_git.st_ino, active_git.st_mode) != (captured_git.st_dev, captured_git.st_ino, captured_git.st_mode):
            changed.add(".git")
        elif _index_bytes(index, dir_fd=cp.git_dir_fd) != (cp.index_backup.read_bytes() if cp.index_existed else None):
            changed.add(".git/index")
    current = {rel: (p, entry) for rel, p, entry in _walk(cp.root, cp.exclude_paths)}
    for rel in cp.entries.keys() | current.keys():
        if rel not in cp.entries or rel not in current:
            changed.add(rel)
            continue
        path, entry = current[rel]
        if not _matches_snapshot(cp, rel, entry, path):
            changed.add(rel)
    return changed
