"""Bounded, byte-exact worktree and Git index checkpoints.

ponytail: Full copies are capped at 50,000 entries and 256 MiB; use a
content-addressed snapshot if larger worktrees need support.
"""

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
    git_identity: object = None

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
            yield str(d.relative_to(root)), d, _Entry("dir", stat.S_IMODE(d.lstat().st_mode))
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
                yield str(p.relative_to(root)), p, _Entry("symlink", target=os.readlink(p))
        for name in filenames:
            p = d / name
            if (d == root and name == ".git") or str(p) in exclude:
                continue
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                entry = _Entry("symlink", target=os.readlink(p))
            elif stat.S_ISREG(st.st_mode):
                entry = _Entry("file", stat.S_IMODE(st.st_mode))
            else:
                raise CheckpointError(f"unsupported special file in worktree: {p}")
            yield str(p.relative_to(root)), p, entry


def _git_metadata(root: Path):
    """Record the root .git identity and resolve a verified index location."""
    link = root / ".git"
    try:
        st = link.lstat()
    except FileNotFoundError:
        return None, None
    identity = (st.st_dev, st.st_ino, st.st_mode)
    if stat.S_ISDIR(st.st_mode):
        gitdir = link
    elif stat.S_ISREG(st.st_mode):
        data = link.read_bytes()
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


def _index_bytes(path: Optional[Path]):
    if path is None:
        return None
    try:
        st = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise CheckpointError(f"unsupported Git index: {path}")
    return path.read_bytes()


def _root_mode(root: Path):
    st = root.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise CheckpointError(f"worktree root is not a real directory: {root}")
    return stat.S_IMODE(st.st_mode)


def capture_checkpoint(root: Path, backup_root: Path, *, exclude_paths: Iterable[Path] = (),
                       max_files: int = MAX_FILES, max_bytes: int = MAX_BYTES) -> WorktreeCheckpoint:
    backup_dir = None
    try:
        root = Path(os.path.abspath(root))
        root_mode = _root_mode(root)
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
        identity, index = _git_metadata(root)
        index_data = _index_bytes(index)
        entries = {}
        total = 0
        for rel, path, entry in _walk(root, exclude):
            entries[rel] = entry
            if len(entries) > max_files:
                raise CheckpointError(f"checkpoint over file ceiling ({max_files})")
            if entry.kind == "file":
                st = path.lstat()
                # ponytail: hardlinks are unsupported; preserve inode groups in a future snapshot format.
                if st.st_nlink > 1:
                    raise CheckpointError(f"unsupported hardlinked file: {path}")
                total += st.st_size
                if total > max_bytes:
                    raise CheckpointError(f"checkpoint over byte ceiling ({max_bytes})")
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix="bluei-ckpt-", dir=backup_root))
        cp = WorktreeCheckpoint(root, backup_dir, entries, exclude, root_mode=root_mode,
                                git_identity=identity, index_target=index, index_existed=index_data is not None)
        for rel, entry in entries.items():
            if entry.kind == "file":
                dest = cp._backup_for(rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, dest)
        if index_data is not None:
            cp.index_backup = backup_dir / "git_index.bin"
            cp.index_backup.write_bytes(index_data)
        return cp
    except (OSError, CheckpointError) as exc:
        if backup_dir is not None:
            try:
                shutil.rmtree(backup_dir)
            except OSError as cleanup:
                raise CheckpointError(f"checkpoint capture failed: {exc}; partial backup retained at {backup_dir}: {cleanup}") from exc
        raise CheckpointError(f"checkpoint capture failed: {exc}") from exc


def _matches_snapshot(cp: WorktreeCheckpoint, rel: str, entry: _Entry, path: Path) -> bool:
    old = cp.entries.get(rel)
    if old is None or old.kind != entry.kind:
        return False
    if entry.kind == "symlink":
        return old.target == entry.target
    if entry.kind == "dir":
        return old.mode == entry.mode
    if old.mode != entry.mode:
        return False
    backup = cp._backup_for(rel)
    if path.stat().st_size != backup.stat().st_size:
        return False
    with path.open("rb") as current, backup.open("rb") as original:
        while True:
            block = current.read(1024 * 1024)
            if block != original.read(1024 * 1024):
                return False
            if not block:
                return True


def restore_checkpoint(cp: WorktreeCheckpoint) -> None:
    """Restore without traversing replacement symlinks or deleting excluded data."""
    try:
        _root_mode(cp.root)
        identity, index = _git_metadata(cp.root)
        if identity != cp.git_identity or index != cp.index_target:
            raise CheckpointError("root Git metadata changed")
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
            path = cp.root / rel
            if entry.kind == "dir":
                previous = cp.entries.get(rel)
                if previous is not None and previous.kind == "dir":
                    continue
                try:
                    path.rmdir()
                except OSError as exc:
                    # A new directory containing excluded paths must remain.
                    if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) and cp.entries.get(rel) is None and any(Path(x).is_relative_to(path) for x in cp.exclude_paths):
                        continue
                    raise
            else:
                path.unlink()
        for rel, entry in sorted(cp.entries.items(), key=lambda item: len(Path(item[0]).parts)):
            if rel in kept:
                continue
            dest = cp.root / rel
            if entry.kind == "dir":
                dest.mkdir(exist_ok=True)
            elif entry.kind == "symlink":
                os.symlink(entry.target, dest)
            else:
                shutil.copyfile(cp._backup_for(rel), dest)
                os.chmod(dest, entry.mode)
        for rel, entry in sorted(cp.entries.items(), key=lambda item: len(Path(item[0]).parts), reverse=True):
            if entry.kind == "dir" and rel not in kept:
                os.chmod(cp.root / rel, entry.mode)
        os.chmod(cp.root, cp.root_mode)
        if index is not None:
            _index_bytes(index)  # reject a substituted symlink or special file
            if cp.index_existed:
                shutil.copyfile(cp.index_backup, index)
            else:
                index.unlink(missing_ok=True)
    except Exception as exc:
        cp.retained = True
        raise CheckpointRestoreError(f"checkpoint restore failed: {exc}; backup retained at {cp.backup_dir}") from exc


def discard_checkpoint(cp: WorktreeCheckpoint) -> None:
    if not cp.retained:
        shutil.rmtree(cp.backup_dir)


def changed_paths(cp: WorktreeCheckpoint) -> set[str]:
    changed = set()
    if _root_mode(cp.root) != cp.root_mode:
        changed.add(".")
    identity, index = _git_metadata(cp.root)
    if identity != cp.git_identity or index != cp.index_target:
        changed.add(".git")
    elif index is not None and _index_bytes(index) != (cp.index_backup.read_bytes() if cp.index_existed else None):
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
