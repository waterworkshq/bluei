"""Persistent storage for learned fix patterns.

JSONL-backed store with three lookup strategies: exact (hash), structural
(abstracted AST hash), and fuzzy (similarity threshold).  Thread-safe via
an internal RLock plus fcntl file lock for cross-process safety.  Patterns
below the deactivation confidence threshold are pruned during rotation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEACTIVATION_THRESHOLD = 0.3
INITIAL_CONFIDENCE = 0.5
MAX_ACTIVE_PATTERNS = 200
PATTERN_STORE_MAX_BYTES = 5 * 1024 * 1024
PATTERN_STORE_MAX_LINES = 10_000


from bluei.engine.jsonl import append_jsonl, read_jsonl
from bluei.engine.models import now_iso  # noqa: E402


def generate_pattern_id() -> str:
    return f"fp-{secrets.token_hex(6)}"


def normalize_snippet(snippet: str) -> str:
    """Dedent, strip trailing whitespace, and collapse runs of spaces/tabs.

    Args:
        snippet: Raw source-code fragment.

    Returns:
        Whitespace-normalized string suitable for hashing and comparison.
    """
    dedented = textwrap.dedent(snippet or "").strip()
    lines = []
    for line in dedented.splitlines():
        lines.append(re.sub(r"[ \t]+", " ", line.rstrip()))
    return "\n".join(lines)


def snippet_hash(snippet: str) -> str:
    normalized = normalize_snippet(snippet)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class FixPattern:
    """A single learned before→after fix template with confidence tracking.

    Attributes:
        pattern_id: Unique identifier (e.g. ``fp-<hex>``).
        rule: Linter rule that triggered the original finding.
        language: Primary language of the affected file.
        file_path: Relative path of the file where the fix was applied.
        before_snippet: Normalized source text before the fix.
        after_snippet: Normalized source text after the fix.
        diff_patch: Full unified diff captured from ``git diff``.
        confidence: Bayesian-style score in [0, 1]; patterns below
            ``DEACTIVATION_THRESHOLD`` are excluded from lookups.
        success_count: Number of times replay confirmed the fix was correct.
        failure_count: Number of times replay produced an incorrect result.
        skip_count: Number of times the pattern was tried but not applicable.
        source: Origin of the pattern (``autofix``, ``claude``, ``contextual``).
        created_at: ISO-8601 UTC timestamp.
        last_used_at: When the pattern was last returned by a lookup.
        last_verified_at: When the pattern last had a successful replay.
        source_finding_ids: Finding IDs that contributed to this pattern.
        framework_constraint: Optional framework name to scope applicability.
        file_pattern: Glob pattern for matching files (default ``**/*``).
        structural_hash: Abstracted AST hash of ``before_snippet``.
    """

    pattern_id: str
    rule: str
    language: str
    file_path: str
    before_snippet: str
    after_snippet: str
    diff_patch: str
    confidence: float = INITIAL_CONFIDENCE
    success_count: int = 0
    failure_count: int = 0
    skip_count: int = 0
    source: str = "autofix"
    created_at: str = field(default_factory=now_iso)
    last_used_at: Optional[str] = None
    last_verified_at: Optional[str] = None
    source_finding_ids: List[str] = field(default_factory=list)
    framework_constraint: Optional[str] = None
    file_pattern: str = "**/*"
    structural_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "rule": self.rule,
            "language": self.language,
            "file_path": self.file_path,
            "before_snippet": self.before_snippet,
            "after_snippet": self.after_snippet,
            "diff_patch": self.diff_patch,
            "confidence": self.confidence,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "skip_count": self.skip_count,
            "source": self.source,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "last_verified_at": self.last_verified_at,
            "source_finding_ids": list(self.source_finding_ids),
            "framework_constraint": self.framework_constraint,
            "file_pattern": self.file_pattern,
            "structural_hash": self.structural_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FixPattern":
        return cls(
            pattern_id=str(data["pattern_id"]),
            rule=str(data["rule"]),
            language=str(data["language"]),
            file_path=str(data["file_path"]),
            before_snippet=str(data["before_snippet"]),
            after_snippet=str(data["after_snippet"]),
            diff_patch=str(data["diff_patch"]),
            confidence=float(data.get("confidence", INITIAL_CONFIDENCE)),
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            skip_count=int(data.get("skip_count", 0)),
            source=str(data.get("source", "autofix")),
            created_at=str(data.get("created_at") or now_iso()),
            last_used_at=data.get("last_used_at"),
            last_verified_at=data.get("last_verified_at"),
            source_finding_ids=list(data.get("source_finding_ids") or []),
            framework_constraint=data.get("framework_constraint"),
            file_pattern=data.get("file_pattern", "**/*"),
            structural_hash=data.get("structural_hash"),
        )


class FixPatternStore:
    """JSONL-backed fix pattern store with an in-memory lookup index.

    Three lookup strategies are supported: exact (SHA-256 hash of normalized
    ``before_snippet``), structural (abstracted AST hash), and fuzzy (similarity
    scoring).  Thread-safe via RLock + fcntl; rotates the JSONL file when it
    exceeds size or line-count limits, pruning deactivated patterns.
    """

    def __init__(self, store_path: Path):
        """Initialize the store and rebuild the in-memory index from disk.

        Args:
            store_path: Path to the ``.jsonl`` file that persists patterns.
        """
        self.store_path = Path(store_path)
        self.index_path = self.store_path.with_name(
            f"{self.store_path.stem}_index.json"
        )
        self.lock_path = self.store_path.with_suffix(self.store_path.suffix + ".lock")
        self._thread_lock = threading.RLock()
        self._patterns: Dict[str, FixPattern] = {}
        self._index: Dict[str, Dict[str, str]] = {}
        self._structural_index: Dict[str, Dict[str, str]] = {}
        self._rebuild_from_disk()

    def lookup(
        self, rule: str, before_snippet: str, framework: Optional[str] = None
    ) -> Optional[FixPattern]:
        """Exact-match lookup by rule + normalized before-snippet hash.

        Args:
            rule: Linter rule name to scope the search.
            before_snippet: Raw source text to match against stored patterns.
            framework: Optional framework filter; skips patterns scoped to a
                different framework.

        Returns:
            Matching ``FixPattern`` with confidence ≥ threshold, or ``None``.
        """
        pattern_id = self._index.get(rule, {}).get(snippet_hash(before_snippet))
        if not pattern_id:
            return None
        pattern = self._patterns.get(pattern_id)
        if not pattern or pattern.confidence < DEACTIVATION_THRESHOLD:
            return None
        if (
            pattern.framework_constraint
            and framework
            and pattern.framework_constraint != framework
        ):
            return None
        return pattern

    def lookup_structural(
        self, rule: str, snippet: str, language: str
    ) -> Optional[FixPattern]:
        """Structural-match lookup using an abstracted AST hash.

        Args:
            rule: Linter rule name to scope the search.
            snippet: Raw source text to hash structurally.
            language: Language hint for the structural hasher.

        Returns:
            Matching ``FixPattern`` with confidence ≥ threshold, or ``None``.
        """
        from bluei.engine.structural_hash import compute_structural_hash

        s_hash = compute_structural_hash(snippet, language)
        pattern_id = self._structural_index.get(rule, {}).get(s_hash)
        if not pattern_id:
            return None
        pattern = self._patterns.get(pattern_id)
        if not pattern or pattern.confidence < DEACTIVATION_THRESHOLD:
            return None
        return pattern

    def lookup_fuzzy(
        self,
        rule: str,
        snippet: str,
        language: str,
        threshold: float = 0.75,
        catalog: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[FixPattern]:
        """Best-effort fuzzy lookup against all active patterns for a rule.

        Args:
            rule: Linter rule name to scope candidates.
            snippet: Raw source text to compare against stored ``before_snippet`` values.
            language: Language hint for the similarity scorer.
            threshold: Minimum similarity score (0–1) to accept a match.
            catalog: Optional rule catalog used to derive a per-rule threshold.

        Returns:
            Highest-scoring ``FixPattern`` above ``threshold``, or ``None``.
        """
        from bluei.engine.structural_hash import (
            fuzzy_structural_match,
            get_fuzzy_threshold,
        )

        effective_threshold = (
            get_fuzzy_threshold(rule, catalog) if catalog else threshold
        )
        candidates = self.load_active(rule=rule)
        best_pattern: Optional[FixPattern] = None
        best_score = 0.0
        for pattern in candidates:
            score = fuzzy_structural_match(snippet, pattern.before_snippet, language)
            if score > best_score and score >= effective_threshold:
                best_score = score
                best_pattern = pattern
        return best_pattern

    def append(self, pattern: FixPattern) -> str:
        """Add a new pattern or merge into an existing duplicate.

        If a pattern with the same rule + normalized ``before_snippet`` already
        exists, increments its ``success_count`` and merges finding IDs instead
        of creating a duplicate.

        Args:
            pattern: The ``FixPattern`` to persist.

        Returns:
            The ``pattern_id`` of the stored (or merged) pattern.

        Raises:
            ValueError: If the active pattern cap (``MAX_ACTIVE_PATTERNS``) is reached.
        """
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            records = self._load_records_unlocked()
            normalized_before = normalize_snippet(pattern.before_snippet)
            existing = self._find_duplicate(records, pattern.rule, normalized_before)
            if existing is not None:
                existing["success_count"] = int(existing.get("success_count", 0)) + 1
                existing_src = existing.get("source_finding_ids") or []
                for fid in pattern.source_finding_ids:
                    if fid and fid not in existing_src:
                        existing_src.append(fid)
                existing["source_finding_ids"] = existing_src
                self._write_records_unlocked(records)
                self._rotate_if_needed_unlocked()
                self._rebuild_from_disk_unlocked()
                self._write_index_unlocked()
                return str(existing["pattern_id"])

            active_count = self._active_count(records)
            if active_count >= MAX_ACTIVE_PATTERNS:
                raise ValueError("active fix pattern cap reached")

            new_pattern = self._prepare_new_pattern(pattern, normalized_before)
            append_jsonl(self.store_path, new_pattern.to_dict())
            self._rotate_if_needed_unlocked()
            self._rebuild_from_disk_unlocked()
            self._write_index_unlocked()
            return new_pattern.pattern_id

    def update_confidence(self, pattern_id: str, delta: float) -> None:
        """Adjust a pattern's confidence score by a signed delta, clamped to [0, 1].

        Args:
            pattern_id: ID of the pattern to update.
            delta: Positive to reinforce, negative to penalize.
        """
        with self._locked():
            records = self._load_records_unlocked()
            found = False
            for record in records:
                if record.get("pattern_id") == pattern_id:
                    current = float(record.get("confidence", INITIAL_CONFIDENCE))
                    record["confidence"] = min(1.0, max(0.0, current + delta))
                    found = True
                    break
            if not found:
                return
            self._write_records_unlocked(records)
            self._rotate_if_needed_unlocked()
            self._rebuild_from_disk_unlocked()
            self._write_index_unlocked()

    def record_replay(self, pattern_id: str, success: bool) -> None:
        """Record the outcome of replaying a pattern against a target file.

        Args:
            pattern_id: ID of the pattern that was replayed.
            success: ``True`` if the replay was confirmed correct, ``False``
                if the result was skipped or inapplicable.
        """
        with self._locked():
            records = self._load_records_unlocked()
            found = False
            timestamp = now_iso()
            for record in records:
                if record.get("pattern_id") == pattern_id:
                    if success:
                        record["success_count"] = (
                            int(record.get("success_count", 0)) + 1
                        )
                        record["last_verified_at"] = timestamp
                    else:
                        record["skip_count"] = int(record.get("skip_count", 0)) + 1
                    record["last_used_at"] = timestamp
                    found = True
                    break
            if not found:
                return
            self._write_records_unlocked(records)
            self._rotate_if_needed_unlocked()
            self._rebuild_from_disk_unlocked()
            self._write_index_unlocked()

    def lookup_by_finding(self, finding_id: str) -> Optional[FixPattern]:
        """Find a pattern whose source_finding_ids contain the given finding."""
        with self._locked():
            for pattern in self._patterns.values():
                if finding_id in pattern.source_finding_ids:
                    return pattern
        return None

    def get_pattern(self, pattern_id: str) -> Optional[FixPattern]:
        """Return a pattern by ID (thread-safe)."""
        with self._locked():
            return self._patterns.get(pattern_id)

    def load_active(self, rule: Optional[str] = None) -> List[FixPattern]:
        """Return all patterns above the deactivation threshold, optionally filtered by rule.

        Args:
            rule: If provided, only return patterns for this linter rule.

        Returns:
            List of active ``FixPattern`` instances.
        """
        patterns = [
            pattern
            for pattern in self._patterns.values()
            if pattern.confidence >= DEACTIVATION_THRESHOLD
        ]
        if rule is not None:
            patterns = [pattern for pattern in patterns if pattern.rule == rule]
        return patterns

    def _prepare_new_pattern(
        self, pattern: FixPattern, normalized_before: str
    ) -> FixPattern:
        """Assign a fresh ID, normalize snippets, and compute the structural hash.

        Args:
            pattern: Raw pattern from the caller.
            normalized_before: Pre-normalized ``before_snippet`` text.

        Returns:
            A ``FixPattern`` ready for serialization with a generated ID.
        """
        from bluei.engine.structural_hash import compute_structural_hash

        return FixPattern(
            pattern_id=generate_pattern_id(),
            rule=pattern.rule,
            language=pattern.language,
            file_path=pattern.file_path,
            before_snippet=normalized_before,
            after_snippet=normalize_snippet(pattern.after_snippet),
            diff_patch=pattern.diff_patch,
            confidence=INITIAL_CONFIDENCE,
            success_count=max(1, int(pattern.success_count or 0)),
            failure_count=max(0, int(pattern.failure_count or 0)),
            skip_count=max(0, int(pattern.skip_count or 0)),
            source=pattern.source,
            created_at=pattern.created_at or now_iso(),
            last_used_at=pattern.last_used_at,
            last_verified_at=pattern.last_verified_at,
            source_finding_ids=list(pattern.source_finding_ids),
            framework_constraint=pattern.framework_constraint,
            file_pattern=pattern.file_pattern,
            structural_hash=compute_structural_hash(
                normalized_before, pattern.language
            ),
        )

    def _rebuild_from_disk(self) -> None:
        with self._locked():
            self._rebuild_from_disk_unlocked()
            self._write_index_unlocked()

    def _rebuild_from_disk_unlocked(self) -> None:
        from bluei.engine.structural_hash import compute_structural_hash

        self._patterns = {}
        self._index = {}
        self._structural_index = {}
        for record in self._load_records_unlocked():
            pattern = FixPattern.from_dict(record)
            self._patterns[pattern.pattern_id] = pattern
            if pattern.confidence < DEACTIVATION_THRESHOLD:
                continue
            before_hash = snippet_hash(pattern.before_snippet)
            self._index.setdefault(pattern.rule, {})[before_hash] = pattern.pattern_id
            s_hash = pattern.structural_hash or compute_structural_hash(
                pattern.before_snippet, pattern.language
            )
            if s_hash:
                self._structural_index.setdefault(pattern.rule, {})[s_hash] = (
                    pattern.pattern_id
                )

    def _write_index_unlocked(self) -> None:
        payload = {
            "version": 1,
            "updated_at": now_iso(),
            "by_rule": self._index,
            "active_count": len(self.load_active()),
            "total_count": len(self._patterns),
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(self.index_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.index_path)

    def _load_records_unlocked(self) -> List[Dict[str, Any]]:
        return read_jsonl(self.store_path)

    def _find_duplicate(
        self,
        records: List[Dict[str, Any]],
        rule: str,
        normalized_before: str,
    ) -> Optional[Dict[str, Any]]:
        """Search records for an existing pattern with the same rule and before-snippet hash.

        Args:
            records: Raw JSONL records loaded from disk.
            rule: Linter rule to match.
            normalized_before: Already-normalized ``before_snippet`` text.

        Returns:
            The matching record dict, or ``None``.
        """
        target_hash = snippet_hash(normalized_before)
        for record in records:
            if record.get("rule") != rule:
                continue
            if snippet_hash(str(record.get("before_snippet", ""))) == target_hash:
                return record
        return None

    def _active_count(self, records: List[Dict[str, Any]]) -> int:
        return sum(
            1
            for record in records
            if float(record.get("confidence", INITIAL_CONFIDENCE))
            >= DEACTIVATION_THRESHOLD
        )

    def _write_records_unlocked(self, records: List[Dict[str, Any]]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        tmp.write_text("", encoding="utf-8")
        for record in records:
            append_jsonl(tmp, record)
        os.replace(tmp, self.store_path)

    def _rotate_if_needed_unlocked(self) -> None:
        if not self.store_path.exists():
            return
        if self.store_path.stat().st_size < PATTERN_STORE_MAX_BYTES:
            try:
                with self.store_path.open("r", encoding="utf-8") as f:
                    line_count = sum(1 for _ in f)
                if line_count < PATTERN_STORE_MAX_LINES:
                    return
            except OSError:
                return
        bak_path = self.store_path.with_suffix(self.store_path.suffix + ".bak")
        try:
            os.replace(self.store_path, bak_path)
        except OSError:
            return
        self._carry_over_active_patterns(bak_path)

    def _carry_over_active_patterns(self, bak_path: Path) -> None:
        """Write active patterns from the backup into a new store file.

        After rotation the live store file no longer exists.  Without this
        step, ``_rebuild_from_disk_unlocked`` would find zero patterns and
        wipe the in-memory index.  We filter to keep only patterns whose
        confidence is at or above the deactivation threshold — deactivated
        patterns are the natural candidates for pruning during rotation.
        """
        active_records: List[Dict[str, Any]] = []
        try:
            with bak_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (
                        float(record.get("confidence", INITIAL_CONFIDENCE))
                        >= DEACTIVATION_THRESHOLD
                    ):
                        active_records.append(record)
        except OSError:
            return
        if active_records:
            self._write_records_unlocked(active_records)

    def _locked(self):
        class _Lock:
            def __init__(self, lock_path: Path):
                self.lock_path = lock_path
                self.handle = None

            def __enter__(self):
                self.outer_lock.acquire()
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = self.lock_path.open("w", encoding="utf-8")
                fcntl.flock(self.handle, fcntl.LOCK_EX)
                return self

            def __exit__(self, exc_type, exc, tb):
                try:
                    if self.handle is not None:
                        fcntl.flock(self.handle, fcntl.LOCK_UN)
                        self.handle.close()
                finally:
                    self.outer_lock.release()

        lock = _Lock(self.lock_path)
        lock.outer_lock = self._thread_lock
        return lock
