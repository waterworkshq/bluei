"""Cross-repo shared pattern library for structural fix reuse.

Stores high-confidence fix patterns keyed by structural hash. Lookup uses
exact hash match first, then fuzzy structural similarity. Patterns are
privacy-normalized before storage (identifiers and literals stripped).
"""

import logging
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_logger = logging.getLogger(__name__)

from bluei.engine.governance import PromotionResult
from bluei.engine.jsonl import read_jsonl
from bluei.engine.structural_hash import (
    compute_structural_hash,
    fuzzy_structural_match,
    get_fuzzy_threshold,
    normalize_for_sharing,
)


MIN_CONFIDENCE_TO_SHARE = 0.8
MIN_REPOS_FOR_LOOKUP = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CrossRepoPattern:
    pattern_id: str
    rule: str
    language: str
    structural_hash: str
    before_snippet: str
    after_snippet: str
    confidence: float
    source_repos: Set[str] = field(default_factory=set)
    success_count: int = 0
    failure_count: int = 0
    created_at: str = field(default_factory=_now_iso)
    last_seen_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "rule": self.rule,
            "language": self.language,
            "structural_hash": self.structural_hash,
            "before_snippet": self.before_snippet,
            "after_snippet": self.after_snippet,
            "confidence": self.confidence,
            "source_repos": sorted(self.source_repos),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CrossRepoPattern":
        repos = data.get("source_repos", [])
        if isinstance(repos, list):
            repos = set(repos)
        elif isinstance(repos, str):
            repos = {repos}
        return cls(
            pattern_id=str(data["pattern_id"]),
            rule=str(data["rule"]),
            language=str(data["language"]),
            structural_hash=str(data["structural_hash"]),
            before_snippet=str(data["before_snippet"]),
            after_snippet=str(data["after_snippet"]),
            confidence=float(data.get("confidence", 0.5)),
            source_repos=repos,
            success_count=int(data.get("success_count", 0)),
            failure_count=int(data.get("failure_count", 0)),
            created_at=str(data.get("created_at", _now_iso())),
            last_seen_at=str(data.get("last_seen_at", _now_iso())),
        )


class SharedPatternLibrary:
    """Language-scoped pattern library shared across repos.

    Stores patterns in per-language JSONL files under library_path.
    Indexed by rule + structural_hash for fast lookup.
    """

    _CACHE_TTL = 600.0

    def __init__(self, library_path: Path):
        self.library_path = Path(library_path)
        self.library_path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: Dict[str, Dict[str, CrossRepoPattern]] = {}
        self._cache_ts: Dict[str, float] = {}

    def publish(
        self,
        rule: str,
        language: str,
        before_snippet: str,
        after_snippet: str,
        confidence: float,
        repo_id: str,
        structural_hash: Optional[str] = None,
        success_count: int = 0,
        failure_count: int = 0,
    ) -> str:
        if confidence < MIN_CONFIDENCE_TO_SHARE:
            return ""
        safe_before = normalize_for_sharing(before_snippet, language)
        safe_after = normalize_for_sharing(after_snippet, language)
        s_hash = structural_hash or compute_structural_hash(before_snippet, language)

        with self._lock:
            records = self._load_language(language)
            key = f"{rule}::{s_hash}"
            existing = records.get(key)

            if existing is not None:
                existing.source_repos.add(repo_id)
                existing.success_count += success_count
                existing.failure_count += failure_count
                existing.confidence = self._merge_confidence(existing, confidence)
                existing.before_snippet = safe_before
                existing.after_snippet = safe_after
                existing.last_seen_at = _now_iso()
            else:
                existing = CrossRepoPattern(
                    pattern_id=f"xp-{rule[:16]}-{s_hash[:8]}",
                    rule=rule,
                    language=language,
                    structural_hash=s_hash,
                    before_snippet=safe_before,
                    after_snippet=safe_after,
                    confidence=confidence,
                    source_repos={repo_id},
                    success_count=success_count,
                    failure_count=failure_count,
                )
                records[key] = existing

            self._flush_language(language, records)
            self._cache.pop(language, None)
            self._cache_ts.pop(language, None)
            return existing.pattern_id

    def lookup(
        self,
        rule: str,
        snippet: str,
        language: str,
        catalog: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[CrossRepoPattern]:
        s_hash = compute_structural_hash(snippet, language)

        with self._lock:
            records = self._get_cached(language)
            key = f"{rule}::{s_hash}"
            exact = records.get(key)
            if exact is not None:
                if len(exact.source_repos) >= MIN_REPOS_FOR_LOOKUP:
                    return exact

            threshold = get_fuzzy_threshold(rule, catalog) if catalog else 0.75
            best: Optional[CrossRepoPattern] = None
            best_score = 0.0
            for pattern in records.values():
                if pattern.rule != rule:
                    continue
                if len(pattern.source_repos) < MIN_REPOS_FOR_LOOKUP:
                    continue
                score = fuzzy_structural_match(
                    snippet, pattern.before_snippet, language
                )
                if score > best_score and score >= threshold:
                    best_score = score
                    best = pattern
            return best

    def publish_composite(
        self,
        pattern: Any,
        repo_id: str,
        language: str,
    ) -> str:
        if pattern.confidence < MIN_CONFIDENCE_TO_SHARE:
            return ""
        with self._lock:
            lang_file = self.library_path / f"{language}_composite.jsonl"
            records = self._load_composite_language(lang_file)
            key = f"{pattern.rule}::{pattern.pattern_id}"
            existing = records.get(key)
            if existing is not None:
                if "source_repos" not in existing:
                    existing["source_repos"] = []
                if repo_id not in existing["source_repos"]:
                    existing["source_repos"].append(repo_id)
                existing["confidence"] = self._merge_confidence_raw(
                    existing.get("confidence", 0.5),
                    pattern.confidence,
                    existing.get("success_count", 0),
                    existing.get("failure_count", 0),
                )
            else:
                import copy

                d = pattern.to_dict()
                d["source_repos"] = [repo_id]
                records[key] = d
            self._flush_composite_language(lang_file, records)
            return key

    def lookup_composite(
        self,
        rule: str,
        language: str,
    ) -> Optional[List[Dict[str, Any]]]:
        with self._lock:
            lang_file = self.library_path / f"{language}_composite.jsonl"
            records = self._load_composite_language(lang_file)
            matches = []
            for rec in records.values():
                if rec.get("rule") == rule and rec.get("confidence", 0) >= 0.5:
                    matches.append(rec)
            return matches if matches else None

    def _load_composite_language(self, lang_file: Path) -> Dict[str, Dict[str, Any]]:
        records: Dict[str, Dict[str, Any]] = {}
        for rec in read_jsonl(lang_file):
            key = f"{rec.get('rule', '')}::{rec.get('pattern_id', '')}"
            records[key] = rec
        return records

    def _flush_composite_language(
        self, lang_file: Path, records: Dict[str, Dict[str, Any]]
    ) -> None:
        tmp = lang_file.with_suffix(".jsonl.tmp")
        lines = []
        for rec in records.values():
            lines.append(json.dumps(rec, sort_keys=True))
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(lang_file)

    def _merge_confidence_raw(
        self, existing_conf: float, new_conf: float, success: int, failure: int
    ) -> float:
        total = success + failure + 1
        weighted = (existing_conf * (total - 1) + new_conf) / total
        return min(1.0, max(0.0, weighted))

    def load_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {"languages": {}, "total_patterns": 0}
        for lang_file in sorted(self.library_path.glob("*.jsonl")):
            lang = lang_file.stem
            count = 0
            repos: Set[str] = set()
            for rec in read_jsonl(lang_file):
                count += 1
                for r in rec.get("source_repos", []):
                    repos.add(r)
            stats["languages"][lang] = {"pattern_count": count, "repos": sorted(repos)}
            stats["total_patterns"] += count
        return stats

    def _merge_confidence(
        self, existing: CrossRepoPattern, new_confidence: float
    ) -> float:
        total = existing.success_count + existing.failure_count + 1
        weighted = (existing.confidence * (total - 1) + new_confidence) / total
        return min(1.0, max(0.0, weighted))

    def _load_language(self, language: str) -> Dict[str, CrossRepoPattern]:
        records: Dict[str, CrossRepoPattern] = {}
        lang_file = self.library_path / f"{language}.jsonl"
        for rec in read_jsonl(lang_file):
            try:
                pat = CrossRepoPattern.from_dict(rec)
            except KeyError:
                continue
            key = f"{pat.rule}::{pat.structural_hash}"
            records[key] = pat
        return records

    def _flush_language(
        self, language: str, records: Dict[str, CrossRepoPattern]
    ) -> None:
        lang_file = self.library_path / f"{language}.jsonl"
        tmp = lang_file.with_suffix(".jsonl.tmp")
        lines = []
        for pat in records.values():
            lines.append(json.dumps(pat.to_dict(), sort_keys=True))
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(lang_file)

    def _get_cached(self, language: str) -> Dict[str, CrossRepoPattern]:
        import time

        now = time.monotonic()
        ts = self._cache_ts.get(language, 0.0)
        if language in self._cache and (now - ts) < self._CACHE_TTL:
            return self._cache[language]
        self._cache[language] = self._load_language(language)
        self._cache_ts[language] = now
        return self._cache[language]


PROMOTION_MIN_REPOS = 5
PROMOTION_MIN_SUCCESS_RATE = 0.95
PROMOTION_MIN_APPLICATIONS = 20
PROMOTION_MAX_RECENT_FAILURES = 0
PROMOTION_RECENT_WINDOW = 10


def check_promotion_eligibility(pattern: CrossRepoPattern) -> bool:
    if len(pattern.source_repos) < PROMOTION_MIN_REPOS:
        return False
    total = pattern.success_count + pattern.failure_count
    if total < PROMOTION_MIN_APPLICATIONS:
        return False
    if total == 0:
        return False
    success_rate = pattern.success_count / total
    if success_rate < PROMOTION_MIN_SUCCESS_RATE:
        return False
    if pattern.failure_count > PROMOTION_MAX_RECENT_FAILURES:
        return False
    return True


def promote_pattern_to_recipe(
    pattern: CrossRepoPattern,
    language: str,
    output_dir: Path,
    config: Optional[Dict[str, Any]] = None,
    repo_state_dir: Optional[Path] = None,
    approval_records_path: Optional[Path] = None,
) -> Optional[PromotionResult]:
    """Promote a cross-repo pattern to a staged recipe YAML.

    Write-producing promotions are routed through the governance gate
    (ADR-0007):

    * ``gate_closed`` — requires a bundle reference; queues an
      ``ApprovalRecord`` (``pending_approval``) and returns
      ``PENDING_APPROVAL`` without writing YAML. No bundle reference →
      ``NOT_ELIGIBLE``.
    * ``gate_open_audit`` (default, including ``config={}``) — writes the
      YAML and appends an ``ApprovalRecord`` (``auto_promote``); returns
      ``PROMOTED``.

    ``approval_records_path=None`` is safe: no audit record is written but
    YAML still is (gate-open) or the queue step is skipped (gate-closed).
    """
    from bluei.engine.governance import (
        ApprovalRecord,
        format_asset_ref,
        resolve_policy,
    )
    from bluei.engine.bundle_loader import has_bundle_reference, load_bundles
    from bluei.engine.jsonl import append_jsonl

    config = config or {}

    if not check_promotion_eligibility(pattern):
        return PromotionResult.NOT_ELIGIBLE

    policy = resolve_policy(config, "pattern", "write_producing")
    asset_ref = format_asset_ref("pattern", pattern.pattern_id)

    if policy == "gate_closed":
        bundles = load_bundles(repo_state_dir) if repo_state_dir else []
        if not has_bundle_reference(asset_ref, bundles):
            return PromotionResult.NOT_ELIGIBLE
        if approval_records_path is not None:
            record = ApprovalRecord(
                asset_ref=asset_ref,
                decision="pending_approval",
                native_state_before="cross-repo-pattern",
                native_state_after="pending-recipe",
                reason="Auto-promotion eligible; gate-closed policy requires approval",
                evidence_snapshot={
                    "pattern_id": pattern.pattern_id,
                    "rule": pattern.rule,
                },
                actor="system:auto-promote",
                timestamp=_now_iso(),
            )
            append_jsonl(approval_records_path, record.to_dict())
        return PromotionResult.PENDING_APPROVAL

    output_dir.mkdir(parents=True, exist_ok=True)

    import yaml

    safe_hash = pattern.structural_hash[:8]
    rule_safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in pattern.rule)
    recipe_id = f"auto-{rule_safe[:32]}-{safe_hash}"

    safety = "idempotent" if pattern.confidence >= 0.95 else "needs_validation"

    recipe_data = {
        "id": recipe_id,
        "rule": pattern.rule,
        "language": language,
        "safety": safety,
        "description": "Auto-promoted from shared pattern library",
        "match": {
            "type": "rule_exact",
            "rule": pattern.rule,
        },
        "replacement": {
            "type": "regex_substitute",
            "pattern": pattern.before_snippet,
            "replacement": pattern.after_snippet,
        },
        "validation": {
            "run_baseline": True,
        },
        "metadata": {
            "version": 1,
            "author": "bluei-auto-promotion",
            "source_pattern_id": pattern.pattern_id,
            "source_repos": sorted(pattern.source_repos),
            "promoted_at": _now_iso(),
        },
    }

    out_path = output_dir / f"{recipe_id}.yaml"
    out_path.write_text(
        yaml.safe_dump(recipe_data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    if approval_records_path is not None:
        record = ApprovalRecord(
            asset_ref=asset_ref,
            decision="auto_promote",
            native_state_before="cross-repo-pattern",
            native_state_after="staged-recipe",
            reason="Auto-promotion via gate-open policy",
            evidence_snapshot={
                "pattern_id": pattern.pattern_id,
                "rule": pattern.rule,
                "recipe_id": recipe_id,
            },
            actor="system:auto-promote",
            timestamp=_now_iso(),
        )
        append_jsonl(approval_records_path, record.to_dict())
    return PromotionResult.PROMOTED


def resume_pending_promotion(
    asset_ref: str,
    approval_records_path: Path,
    output_dir: Path,
) -> PromotionResult:
    """Write a Recipe YAML that was cached as ``pending_approval``.

    Reads the most-recent ``pending_approval`` record for ``asset_ref`` from
    the JSONL trail, extracts ``recipe_id`` + ``proposed_recipe_yaml`` from
    its ``evidence_snapshot``, writes the YAML to
    ``output_dir/<recipe_id>.yaml``, and appends an ``auto_promote``
    ``ApprovalRecord`` (ADR-0020 approval-loop closer).

    Returns:
        ``PROMOTED`` on success, ``NOT_ELIGIBLE`` if no pending record exists
        for ``asset_ref`` or the cached YAML is missing.
    """
    from bluei.engine.governance import ApprovalRecord
    from bluei.engine.jsonl import append_jsonl, read_jsonl

    records = list(read_jsonl(approval_records_path))
    pending: Optional[Dict[str, Any]] = None
    for rec in reversed(records):
        if (
            rec.get("asset_ref") == asset_ref
            and rec.get("decision") == "pending_approval"
        ):
            pending = rec
            break

    if pending is None:
        return PromotionResult.NOT_ELIGIBLE

    evidence = pending.get("evidence_snapshot", {}) or {}
    recipe_id = evidence.get("recipe_id")
    cached_yaml = evidence.get("proposed_recipe_yaml")
    if not recipe_id or not cached_yaml:
        return PromotionResult.NOT_ELIGIBLE

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{recipe_id}.yaml"
    out_path.write_text(cached_yaml, encoding="utf-8")

    append_jsonl(
        approval_records_path,
        ApprovalRecord(
            asset_ref=asset_ref,
            decision="auto_promote",
            native_state_before="pending-recipe",
            native_state_after="staged-recipe",
            reason="Resumed promotion of Foundry-cached recipe",
            evidence_snapshot={
                "recipe_id": recipe_id,
                "source_pattern_id": evidence.get("source_pattern_id"),
                "source_bundle_id": evidence.get("source_bundle_id"),
            },
            actor="system:resume-pending-promotion",
            timestamp=_now_iso(),
        ).to_dict(),
    )
    return PromotionResult.PROMOTED
