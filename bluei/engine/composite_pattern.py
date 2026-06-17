"""Multi-file composite fix patterns — matching and applying fixes across files atomically.

Manages ordered sequences of regex replacements that may touch multiple files
in a single fix.  Each composite pattern is stored as a JSONL record with
per-step scope (finding_location, file_header, related_file) and supports
automatic rollback on failure.
"""

from __future__ import annotations

import logging
import fcntl
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

from bluei.engine.jsonl import append_jsonl, read_jsonl
from bluei.engine.utils import run_capture


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _generate_composite_id() -> str:
    import uuid

    return f"comp-{uuid.uuid4().hex[:12]}"


@dataclass
class CompositePatternStep:
    """A single regex replacement within a composite fix."""

    order: int
    description: str
    match: str
    replacement: str
    scope: str
    file_pattern: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "order": self.order,
            "description": self.description,
            "match": self.match,
            "replacement": self.replacement,
            "scope": self.scope,
        }
        if self.file_pattern is not None:
            d["file_pattern"] = self.file_pattern
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CompositePatternStep:
        return cls(
            order=int(data["order"]),
            description=str(data["description"]),
            match=str(data["match"]),
            replacement=str(data["replacement"]),
            scope=str(data["scope"]),
            file_pattern=data.get("file_pattern"),
        )


@dataclass
class CompositePattern:
    """An ordered sequence of steps that together form one atomic fix across files."""

    pattern_id: str
    rule: str
    language: str
    steps: List[CompositePatternStep] = field(default_factory=list)
    confidence: float = 0.5
    success_count: int = 0
    failure_count: int = 0
    source: str = "extracted"
    created_at: str = field(default_factory=_now_iso)
    last_used_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "composite",
            "pattern_id": self.pattern_id,
            "rule": self.rule,
            "language": self.language,
            "steps": [s.to_dict() for s in self.steps],
            "confidence": self.confidence,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "source": self.source,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CompositePattern:
        steps = [CompositePatternStep.from_dict(s) for s in data.get("steps", [])]
        return cls(
            pattern_id=str(data["pattern_id"]),
            rule=str(data["rule"]),
            language=str(data["language"]),
            steps=steps,
            confidence=float(data.get("confidence", 0.5)),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            source=str(data.get("source", "extracted")),
            created_at=str(data.get("created_at", _now_iso())),
            last_used_at=data.get("last_used_at"),
        )


class CompositePatternApplier:
    def apply(
        self,
        pattern: CompositePattern,
        finding_path: str,
        worktree: Path,
        log_file: Optional[Path] = None,
    ) -> Tuple[bool, List[str], Optional[str]]:
        """Execute all steps of a composite pattern, rolling back on failure.

        Args:
            pattern: The composite pattern containing ordered fix steps.
            finding_path: Relative path of the finding file within worktree.
            worktree: Root of the working tree to apply fixes in.
            log_file: Optional path for append-only log output.

        Returns:
            Tuple of (success, list of modified relative paths, error message or None).
        """
        snapshots: Dict[str, str] = {}
        modified_files: List[str] = []

        for step in sorted(pattern.steps, key=lambda s: s.order):
            target_file = self._resolve_target(step, finding_path, worktree)
            if target_file is None:
                self._rollback(snapshots, worktree)
                return (
                    False,
                    [],
                    f"no file matches scope={step.scope} file_pattern={step.file_pattern}",
                )

            rel = str(target_file.relative_to(worktree))
            if rel not in snapshots:
                try:
                    snapshots[rel] = target_file.read_text(encoding="utf-8")
                except Exception as exc:
                    self._rollback(snapshots, worktree)
                    return False, [], f"cannot read {rel}: {exc}"

            content = target_file.read_text(encoding="utf-8")
            try:
                new_content = self._apply_step(step, content, finding_path)
            except re.error as exc:
                self._rollback(snapshots, worktree)
                return False, [], f"regex error in step {step.order}: {exc}"

            try:
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(new_content, encoding="utf-8")
            except Exception as exc:
                self._rollback(snapshots, worktree)
                return False, [], f"write error in step {step.order}: {exc}"

            if rel not in modified_files:
                modified_files.append(rel)

        return True, modified_files, None

    def _resolve_target(
        self, step: CompositePatternStep, finding_path: str, worktree: Path
    ) -> Optional[Path]:
        """Resolve a step's scope to a concrete file path inside the worktree.

        Args:
            step: The step whose scope and optional file_pattern determine the target.
            finding_path: Relative path of the original finding.
            worktree: Working tree root.

        Returns:
            Absolute Path to the target file, or None if unresolvable.
        """
        if step.scope == "finding_location":
            return worktree / finding_path
        elif step.scope == "file_header":
            return worktree / finding_path
        elif step.scope == "related_file":
            if not step.file_pattern:
                return None
            if ".." in step.file_pattern or step.file_pattern.startswith("/"):
                return None
            matches = list(worktree.glob(step.file_pattern))
            for m in matches:
                try:
                    m.resolve().relative_to(worktree.resolve())
                except ValueError:
                    continue
                return m
            return None
        return None

    _MAX_REGEX_LEN = 2000

    def _apply_step(
        self, step: CompositePatternStep, content: str, finding_path: str
    ) -> str:
        """Apply a single step's regex substitution to file content.

        For ``file_header`` scope, only the first line is rewritten.
        Patterns exceeding ``_MAX_REGEX_LEN`` raise ``re.error``.

        Args:
            step: Step containing match pattern, replacement, and scope.
            content: Current file content.
            finding_path: Relative path (unused, reserved for future scoping).

        Returns:
            The file content after substitution.
        """
        if len(step.match) > self._MAX_REGEX_LEN:
            raise re.error(f"pattern too long ({len(step.match)} chars)")
        if step.scope == "file_header":
            lines = content.split("\n")
            if lines:
                lines[0] = re.sub(step.match, step.replacement, lines[0])
                return "\n".join(lines)
            return re.sub(step.match, step.replacement, content)

        return re.sub(step.match, step.replacement, content)

    def _rollback(self, snapshots: Dict[str, str], worktree: Path) -> None:
        """Restore all snapshotted files to their pre-fix state.

        Falls back to ``git checkout --`` for files that cannot be written.

        Args:
            snapshots: Mapping of relative path → original file content.
            worktree: Working tree root.
        """
        for rel, original in snapshots.items():
            target = worktree / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(original, encoding="utf-8")
            except Exception:
                _logger.debug("Failed to restore file snapshot")
        try:
            paths = list(snapshots.keys())
            if paths:
                run_capture(
                    ["git", "checkout", "--"] + paths,
                    cwd=worktree,
                    timeout=10,
                )
        except Exception:
            _logger.debug("Failed to git checkout files")


class CompositePatternStore:
    def __init__(self, store_path: Path):
        self.store_path = Path(store_path)
        self.lock_path = self.store_path.with_suffix(self.store_path.suffix + ".lock")
        self._thread_lock = threading.RLock()
        self._composites: Dict[str, CompositePattern] = {}
        self._by_rule: Dict[str, List[str]] = {}
        self._rebuild()

    def append(self, pattern: CompositePattern) -> str:
        """Append a new composite pattern to the JSONL store.

        Assigns a generated ID if ``pattern.pattern_id`` is empty.

        Args:
            pattern: The composite pattern to persist.

        Returns:
            The pattern ID (existing or newly generated).
        """
        with self._locked():
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            if not pattern.pattern_id:
                pattern.pattern_id = _generate_composite_id()
            record = pattern.to_dict()
            append_jsonl(self.store_path, record)
            self._composites[pattern.pattern_id] = pattern
            self._by_rule.setdefault(pattern.rule, []).append(pattern.pattern_id)
            return pattern.pattern_id

    def lookup(self, rule: str) -> Optional[CompositePattern]:
        """Find the first composite pattern for a rule with confidence >= 0.5.

        Args:
            rule: Rule identifier (e.g. ``"ruff-e501"``).

        Returns:
            The best matching pattern, or None if none qualify.
        """
        ids = self._by_rule.get(rule, [])
        for pid in ids:
            pat = self._composites.get(pid)
            if pat and pat.confidence >= 0.5:
                return pat
        return None

    def get(self, pattern_id: str) -> Optional[CompositePattern]:
        return self._composites.get(pattern_id)

    def record_result(self, pattern_id: str, success: bool) -> None:
        """Record a fix outcome and adjust pattern confidence accordingly.

        Success bumps confidence by +0.05 (capped at 1.0); failure drops it
        by −0.1 (floored at 0.0).  Rewrites the entire JSONL store.

        Args:
            pattern_id: ID of the pattern that was applied.
            success: Whether the fix succeeded.
        """
        with self._locked():
            pat = self._composites.get(pattern_id)
            if pat is None:
                return
            if success:
                pat.success_count += 1
                pat.confidence = min(1.0, pat.confidence + 0.05)
            else:
                pat.failure_count += 1
                pat.confidence = max(0.0, pat.confidence - 0.1)
            pat.last_used_at = _now_iso()
            self._rewrite_all()

    def _locked(self):
        """Context manager that acquires both a thread-level RLock and an fcntl file lock."""
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            self._thread_lock.acquire()
            try:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.lock_path.open("w", encoding="utf-8")
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()
            finally:
                self._thread_lock.release()

        return _ctx()

    def _rebuild(self) -> None:
        """Reload the in-memory index from the JSONL store file."""
        self._composites = {}
        self._by_rule = {}
        for record in read_jsonl(self.store_path):
            if record.get("type") != "composite":
                continue
            pat = CompositePattern.from_dict(record)
            self._composites[pat.pattern_id] = pat
            self._by_rule.setdefault(pat.rule, []).append(pat.pattern_id)

    def _rewrite_all(self) -> None:
        """Atomically rewrite the entire JSONL store from the in-memory index."""
        import os

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        records = []
        for pat in self._composites.values():
            records.append(json.dumps(pat.to_dict(), sort_keys=True))
        tmp = self.store_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(r + "\n")
        os.replace(str(tmp), str(self.store_path))
