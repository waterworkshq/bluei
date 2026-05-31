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
                score = fuzzy_structural_match(snippet, pattern.before_snippet, language)
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
                    existing.get("confidence", 0.5), pattern.confidence,
                    existing.get("success_count", 0), existing.get("failure_count", 0),
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
        if not lang_file.exists():
            return records
        try:
            for line in lang_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    key = f"{rec.get('rule', '')}::{rec.get('pattern_id', '')}"
                    records[key] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            _logger.debug("Failed to read pattern library records")
        return records

    def _flush_composite_language(self, lang_file: Path, records: Dict[str, Dict[str, Any]]) -> None:
        tmp = lang_file.with_suffix(".jsonl.tmp")
        lines = []
        for rec in records.values():
            lines.append(json.dumps(rec, sort_keys=True))
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(lang_file)

    def _merge_confidence_raw(self, existing_conf: float, new_conf: float,
                              success: int, failure: int) -> float:
        total = success + failure + 1
        weighted = (existing_conf * (total - 1) + new_conf) / total
        return min(1.0, max(0.0, weighted))

    def load_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {"languages": {}, "total_patterns": 0}
        for lang_file in sorted(self.library_path.glob("*.jsonl")):
            lang = lang_file.stem
            count = 0
            repos: Set[str] = set()
            try:
                for line in lang_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    count += 1
                    for r in rec.get("source_repos", []):
                        repos.add(r)
            except (json.JSONDecodeError, OSError):
                _logger.debug("Failed to read shared pattern stats")
            stats["languages"][lang] = {"pattern_count": count, "repos": sorted(repos)}
            stats["total_patterns"] += count
        return stats

    def _merge_confidence(self, existing: CrossRepoPattern, new_confidence: float) -> float:
        total = existing.success_count + existing.failure_count + 1
        weighted = (existing.confidence * (total - 1) + new_confidence) / total
        return min(1.0, max(0.0, weighted))

    def _load_language(self, language: str) -> Dict[str, CrossRepoPattern]:
        records: Dict[str, CrossRepoPattern] = {}
        lang_file = self.library_path / f"{language}.jsonl"
        if not lang_file.exists():
            return records
        try:
            for line in lang_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    pat = CrossRepoPattern.from_dict(rec)
                    key = f"{pat.rule}::{pat.structural_hash}"
                    records[key] = pat
                except (json.JSONDecodeError, KeyError):
                    continue
        except OSError:
            _logger.debug("Failed to read cross-repo patterns")
        return records

    def _flush_language(self, language: str, records: Dict[str, CrossRepoPattern]) -> None:
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
) -> Optional[Path]:
    if not check_promotion_eligibility(pattern):
        return None

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
    out_path.write_text(yaml.safe_dump(recipe_data, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return out_path
