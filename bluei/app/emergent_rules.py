"""Emergent rule proposal primitives.

This module records repeated finding patterns that may become rules later.
Detection helpers are shadow-only: they collect evidence but do not affect
health scores, suppress findings, or activate rules.
"""

from __future__ import annotations

import logging
import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from bluei.engine.models import Finding, now_iso
from bluei.engine.report import _infer_category, infer_language_from_path
from .state import _atomic_json_write

_logger = logging.getLogger(__name__)


class EmergentRuleStatus(str, Enum):
    PROPOSED = "proposed"
    CANDIDATE = "candidate"
    TENTATIVE = "tentative"
    ACTIVE = "active"
    REJECTED = "rejected"
    RETIRED = "retired"


class DetectionType(str, Enum):
    TEXT_PATTERN = "text_pattern"
    STRUCTURAL = "structural"
    COMPOSITE = "composite"


@dataclass
class DetectionPattern:
    detection_type: DetectionType = DetectionType.TEXT_PATTERN
    search_pattern: str = ""
    file_glob: str = "**/*"
    context_lines: int = 2
    ast_pattern: str | None = None
    composite_signals: List[str] = field(default_factory=list)
    composite_logic: str = "all"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["detection_type"] = self.detection_type.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DetectionPattern":
        payload = dict(data)
        payload.setdefault("detection_type", DetectionType.TEXT_PATTERN.value)
        payload.setdefault("file_glob", "**/*")
        payload.setdefault("context_lines", 2)
        payload.setdefault("ast_pattern", None)
        payload.setdefault("composite_signals", [])
        payload.setdefault("composite_logic", "all")
        payload["detection_type"] = DetectionType(payload["detection_type"])
        return cls(**payload)


@dataclass
class EmergentRule:
    rule_id: str
    header: str
    detection_pattern: DetectionPattern
    language: str
    category: str
    status: EmergentRuleStatus = EmergentRuleStatus.PROPOSED
    evidence_runs: List[str] = field(default_factory=list)
    source_finding_ids: List[str] = field(default_factory=list)
    observation_count: int = 0
    false_positive_rate: float = 0.0
    shadow_runs: int = 0
    shadow_findings: int = 0
    false_positive_count: int = 0
    confidence: float = 0.0
    severity_default: str = "low"
    max_severity: str = "medium"
    can_suppress: bool = False
    component_override: str | None = None
    weight_multiplier: float = 0.5
    created_at: str | None = None
    updated_at: str | None = None
    activated_at: str | None = None
    retired_at: str | None = None
    rejected_reason: str | None = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["detection_pattern"] = self.detection_pattern.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmergentRule":
        payload = dict(data)
        payload["detection_pattern"] = DetectionPattern.from_dict(
            payload.get("detection_pattern", {})
        )
        payload["status"] = EmergentRuleStatus(payload.get("status", "proposed"))
        payload.setdefault("evidence_runs", [])
        payload.setdefault("source_finding_ids", [])
        payload.setdefault("observation_count", 0)
        payload.setdefault("false_positive_rate", 0.0)
        payload.setdefault("shadow_runs", 0)
        payload.setdefault("shadow_findings", 0)
        payload.setdefault("false_positive_count", 0)
        payload.setdefault("confidence", 0.0)
        payload.setdefault("severity_default", "low")
        payload.setdefault("max_severity", "medium")
        payload.setdefault("can_suppress", False)
        payload.setdefault("component_override", None)
        payload.setdefault("weight_multiplier", 0.5)
        payload.setdefault("created_at", None)
        payload.setdefault("updated_at", None)
        payload.setdefault("activated_at", None)
        payload.setdefault("retired_at", None)
        payload.setdefault("rejected_reason", None)
        payload.setdefault("notes", "")
        return cls(**payload)


@dataclass
class ShadowRuleMatch:
    rule_id: str
    path: str
    line: int
    snippet: str
    status: EmergentRuleStatus

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "path": self.path,
            "line": self.line,
            "snippet": self.snippet,
            "status": self.status.value,
            "source": "emergent-shadow",
        }


class EmergentRuleStore:
    """Persistent store for emergent rules backed by a JSON file.

    Manages the lifecycle of rules from proposal through shadow validation
    to activation, retirement, or rejection.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> List[EmergentRule]:
        """Load all rules from the backing JSON file.

        Returns:
            List of EmergentRule instances (empty list if file is missing).
        """
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        return [EmergentRule.from_dict(rule) for rule in payload.get("rules", [])]

    def save(self, rules: List[EmergentRule]) -> None:
        """Persist rules to disk along with summary counts.

        Args:
            rules: Complete list of rules to serialize.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        proposed_count = sum(
            1 for rule in rules if rule.status == EmergentRuleStatus.PROPOSED
        )
        active_count = sum(
            1 for rule in rules if rule.status == EmergentRuleStatus.ACTIVE
        )
        _atomic_json_write(
            self.path,
            {
                "version": 1,
                "updated_at": now_iso(),
                "rules": [rule.to_dict() for rule in rules],
                "proposed_count": proposed_count,
                "active_count": active_count,
            },
        )

    def observe_findings(
        self,
        findings: List[Finding],
        *,
        run_id: str,
        min_observations: int = 5,
        existing_rule_ids: set[str] | None = None,
    ) -> Dict[str, int]:
        """Match findings against known patterns and create or update rule proposals.

        Args:
            findings: Findings from a single scan run.
            run_id: Identifier of the originating run.
            min_observations: Minimum finding repetitions to propose a rule.
            existing_rule_ids: Rule IDs already covered by plugins; matching
                findings are skipped.

        Returns:
            Dict with keys ``created``, ``updated``, and ``skipped`` counts.
        """
        existing_rule_ids = existing_rule_ids or set()
        rules = self.load()
        by_key = {
            _proposal_key(
                rule.detection_pattern.search_pattern, rule.detection_pattern.file_glob
            ): rule
            for rule in rules
        }
        created = 0
        updated = 0
        skipped = 0
        proposals = propose_rules_from_findings(
            findings,
            run_id=run_id,
            min_observations=min_observations,
            existing_rule_ids=existing_rule_ids,
        )
        proposed_keys = {
            _proposal_key(
                rule.detection_pattern.search_pattern, rule.detection_pattern.file_glob
            )
            for rule in proposals
        }
        for rule in proposals:
            key = _proposal_key(
                rule.detection_pattern.search_pattern,
                rule.detection_pattern.file_glob,
            )
            existing = by_key.get(key)
            if existing is None:
                rules.append(rule)
                by_key[key] = rule
                created += 1
            else:
                _merge_observation(existing, rule, run_id)
                updated += 1
        for finding in findings:
            key = _proposal_key(finding.rule, _directory_glob(finding.path))
            if finding.rule in existing_rule_ids or key not in proposed_keys:
                skipped += 1
        self.save(rules)
        return {"created": created, "updated": updated, "skipped": skipped}

    def observe_fix_patterns(
        self,
        patterns: List[Any],
        *,
        run_id: str,
        min_success_count: int = 5,
    ) -> Dict[str, int]:
        """Create or update rules from repeated successful fix patterns.

        Args:
            patterns: Fix pattern objects with ``success_count`` and
                ``confidence`` attributes.
            run_id: Identifier of the originating run.
            min_success_count: Minimum successful applications to propose a rule.

        Returns:
            Dict with keys ``created``, ``updated``, and ``skipped`` counts.
        """
        rules = self.load()
        by_key = {
            _proposal_key(
                rule.detection_pattern.search_pattern, rule.detection_pattern.file_glob
            ): rule
            for rule in rules
        }
        created = 0
        updated = 0
        proposals = propose_rules_from_fix_patterns(
            patterns,
            run_id=run_id,
            min_success_count=min_success_count,
        )
        for rule in proposals:
            key = _proposal_key(
                rule.detection_pattern.search_pattern,
                rule.detection_pattern.file_glob,
            )
            existing = by_key.get(key)
            if existing is None:
                rules.append(rule)
                by_key[key] = rule
                created += 1
            else:
                _merge_observation(existing, rule, run_id)
                updated += 1
        skipped = max(0, len(patterns) - len(proposals))
        self.save(rules)
        return {"created": created, "updated": updated, "skipped": skipped}

    def validate_proposals(
        self,
        *,
        existing_rule_ids: set[str] | None = None,
    ) -> Dict[str, int]:
        """Promote PROPOSED rules to CANDIDATE or reject those covered by existing plugins.

        Args:
            existing_rule_ids: Set of rule IDs already handled by plugins;
                matching proposals are rejected.

        Returns:
            Dict with keys ``candidate``, ``rejected``, and ``unchanged`` counts.
        """
        existing_rule_ids = existing_rule_ids or set()
        candidate = 0
        rejected = 0
        unchanged = 0
        rules = self.load()
        for rule in rules:
            if rule.status != EmergentRuleStatus.PROPOSED:
                unchanged += 1
                continue
            if rule.detection_pattern.search_pattern in existing_rule_ids:
                rule.status = EmergentRuleStatus.REJECTED
                rule.rejected_reason = "covered_by_existing_rule"
                rule.updated_at = now_iso()
                rejected += 1
                continue
            rule.status = EmergentRuleStatus.CANDIDATE
            rule.rejected_reason = None
            rule.updated_at = now_iso()
            candidate += 1
        self.save(rules)
        return {"candidate": candidate, "rejected": rejected, "unchanged": unchanged}

    def record_shadow_run(
        self,
        rule_id: str,
        *,
        matches: int,
        false_positives: int,
        min_shadow_runs: int = 3,
        max_false_positive_rate: float = 0.2,
    ) -> Dict[str, Any]:
        """Record the result of a shadow validation run for a single rule.

        Transitions CANDIDATE → TENTATIVE on first call.  After enough shadow
        runs the rule is promoted to ACTIVE or rejected based on false-positive
        rate.

        Args:
            rule_id: ID of the rule to update.
            matches: Number of shadow matches found this run.
            false_positives: Number of confirmed false positives this run.
            min_shadow_runs: Runs required before promotion/rejection decision.
            max_false_positive_rate: Threshold above which the rule is rejected.

        Returns:
            Dict with ``status``, ``updated``, ``shadow_runs``, and
            ``false_positive_rate``.

        Raises:
            KeyError: If ``rule_id`` is not found in the store.
        """
        rules = self.load()
        for rule in rules:
            if rule.rule_id != rule_id:
                continue
            if rule.status == EmergentRuleStatus.CANDIDATE:
                rule.status = EmergentRuleStatus.TENTATIVE
            if rule.status != EmergentRuleStatus.TENTATIVE:
                return {"status": rule.status.value, "updated": False}
            rule.shadow_runs += 1
            rule.shadow_findings += max(0, matches)
            rule.false_positive_count += max(0, false_positives)
            rule.false_positive_rate = (
                rule.false_positive_count / rule.shadow_findings
                if rule.shadow_findings
                else 0.0
            )
            if (
                rule.shadow_runs >= min_shadow_runs
                and rule.false_positive_rate <= max_false_positive_rate
            ):
                rule.status = EmergentRuleStatus.ACTIVE
                rule.activated_at = now_iso()
            elif (
                rule.shadow_runs >= min_shadow_runs
                and rule.false_positive_rate > max_false_positive_rate
            ):
                rule.status = EmergentRuleStatus.REJECTED
                rule.rejected_reason = "shadow_false_positive_rate"
            rule.updated_at = now_iso()
            self.save(rules)
            return {
                "status": rule.status.value,
                "updated": True,
                "shadow_runs": rule.shadow_runs,
                "false_positive_rate": rule.false_positive_rate,
            }
        raise KeyError(rule_id)

    def reject_rule(self, rule_id: str, *, reason: str = "manual") -> Dict[str, Any]:
        """Manually reject a rule.

        Args:
            rule_id: ID of the rule to reject.
            reason: Human-readable rejection reason.

        Returns:
            Dict with ``status`` and ``updated`` flag.

        Raises:
            KeyError: If ``rule_id`` is not found.
        """
        rules = self.load()
        for rule in rules:
            if rule.rule_id != rule_id:
                continue
            rule.status = EmergentRuleStatus.REJECTED
            rule.rejected_reason = reason
            rule.updated_at = now_iso()
            self.save(rules)
            return {"status": rule.status.value, "updated": True}
        raise KeyError(rule_id)

    def approve_rule(self, rule_id: str) -> Dict[str, Any]:
        """Manually activate a rule, bypassing shadow validation.

        Args:
            rule_id: ID of the rule to approve.

        Returns:
            Dict with ``status`` and ``updated`` flag.  Includes ``reason``
            if the current status does not allow approval.

        Raises:
            KeyError: If ``rule_id`` is not found.
        """
        rules = self.load()
        for rule in rules:
            if rule.rule_id != rule_id:
                continue
            if rule.status not in (
                EmergentRuleStatus.PROPOSED,
                EmergentRuleStatus.CANDIDATE,
                EmergentRuleStatus.TENTATIVE,
            ):
                return {
                    "status": rule.status.value,
                    "updated": False,
                    "reason": f"cannot approve from {rule.status.value}",
                }
            rule.status = EmergentRuleStatus.ACTIVE
            rule.activated_at = now_iso()
            rule.updated_at = now_iso()
            self.save(rules)
            return {"status": rule.status.value, "updated": True}
        raise KeyError(rule_id)

    def retire_rule(self, rule_id: str, *, reason: str = "manual") -> Dict[str, Any]:
        """Retire an active rule so it no longer produces findings.

        Args:
            rule_id: ID of the rule to retire.
            reason: Human-readable retirement reason.

        Returns:
            Dict with ``status`` and ``updated`` flag.

        Raises:
            KeyError: If ``rule_id`` is not found.
        """
        rules = self.load()
        for rule in rules:
            if rule.rule_id != rule_id:
                continue
            rule.status = EmergentRuleStatus.RETIRED
            rule.rejected_reason = reason
            rule.retired_at = now_iso()
            rule.updated_at = now_iso()
            self.save(rules)
            return {"status": rule.status.value, "updated": True}
        raise KeyError(rule_id)

    def promote_rule(
        self, rule_id: str, plugin_dir: Optional[Path] = None
    ) -> Dict[str, Any]:
        """Retire an emergent rule and optionally write it into a plugin manifest.

        Args:
            rule_id: ID of the rule to promote.
            plugin_dir: Directory containing ``plugin.yaml``; if provided the
                rule is appended to the manifest's discovery rules.

        Returns:
            Dict with ``promoted``, ``rule_id``, ``plugin_written``, and
            optional ``reason`` or ``plugin_dir``.

        Raises:
            KeyError: If ``rule_id`` is not found.
        """
        import yaml

        rules = self.load()
        target = None
        for r in rules:
            if r.rule_id == rule_id:
                target = r
                break
        if target is None:
            raise KeyError(rule_id)
        if target.status.value not in ("active", "tentative"):
            return {
                "promoted": False,
                "reason": f"cannot promote from {target.status.value}",
            }

        target.status = EmergentRuleStatus.RETIRED
        target.retired_at = now_iso()
        target.updated_at = now_iso()
        self.save(rules)

        if plugin_dir is None:
            return {
                "promoted": True,
                "rule_id": rule_id,
                "plugin_written": False,
                "reason": "no plugin_dir",
            }

        plugin_yaml = plugin_dir / "plugin.yaml"
        manifest = {}
        if plugin_yaml.exists():
            try:
                with open(plugin_yaml) as f:
                    manifest = yaml.safe_load(f) or {}
            except Exception:
                manifest = {}

        discovery = manifest.setdefault("discovery", {})
        rules_list = discovery.setdefault("rules", [])
        new_entry = {
            "id": target.rule_id,
            "category": "lint",
            "confidence": 0.85,
            "autofix": False,
            "promoted_from": "emergent",
            "evidence_count": target.observation_count,
        }
        rules_list.append(new_entry)

        tmp_path = plugin_yaml.with_suffix(".yaml.tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(manifest, f, default_flow_style=False)
        tmp_path.replace(plugin_yaml)

        return {
            "promoted": True,
            "rule_id": rule_id,
            "plugin_written": True,
            "plugin_dir": str(plugin_dir),
        }

    def retire_stale_rules(
        self, *, active_days: int = 60, proposed_days: int = 30, max_active: int = 50
    ) -> Dict[str, int]:
        """Retire rules that have exceeded their age or count limits.

        Args:
            active_days: Days after which an ACTIVE rule is retired.
            proposed_days: Days after which a PROPOSED rule is retired.
            max_active: Cap on concurrent ACTIVE rules; oldest are retired first.

        Returns:
            Dict with ``retired`` count.
        """
        from datetime import datetime, timedelta, timezone

        rules = self.load()
        now = datetime.now(timezone.utc)
        retired = 0

        for rule in rules:
            updated = rule.updated_at or rule.created_at or now.isoformat()
            try:
                updated_dt = datetime.fromisoformat(updated)
            except (ValueError, TypeError):
                continue
            age_days = (now - updated_dt).days
            if rule.status == EmergentRuleStatus.ACTIVE and age_days > active_days:
                rule.status = EmergentRuleStatus.RETIRED
                rule.retired_at = now.isoformat()
                rule.updated_at = now.isoformat()
                retired += 1
                continue
            if rule.status == EmergentRuleStatus.PROPOSED and age_days > proposed_days:
                rule.status = EmergentRuleStatus.RETIRED
                rule.retired_at = now.isoformat()
                rule.updated_at = now.isoformat()
                retired += 1

        active_count = sum(1 for r in rules if r.status == EmergentRuleStatus.ACTIVE)
        if active_count > max_active:
            active_rules = [r for r in rules if r.status == EmergentRuleStatus.ACTIVE]
            active_rules.sort(key=lambda r: r.updated_at or "", reverse=True)
            for rule in active_rules[max_active:]:
                rule.status = EmergentRuleStatus.RETIRED
                rule.retired_at = now.isoformat()
                rule.updated_at = now.isoformat()
                retired += 1

        self.save(rules)
        return {"retired": retired}


def scan_shadow_rules(
    rules: Iterable[EmergentRule],
    worktree: Path,
) -> List[ShadowRuleMatch]:
    """Scan a worktree for matches against candidate/tentative/active rules.

    Only TEXT_PATTERN rules are scanned; structural and composite types are
    skipped.

    Args:
        rules: Emergent rules to evaluate.
        worktree: Root directory of the repository to scan.

    Returns:
        List of ShadowRuleMatch instances for every matching line.
    """
    root = Path(worktree)
    matches: List[ShadowRuleMatch] = []
    shadowable_statuses = {
        EmergentRuleStatus.CANDIDATE,
        EmergentRuleStatus.TENTATIVE,
        EmergentRuleStatus.ACTIVE,
    }
    for rule in rules:
        pattern = rule.detection_pattern
        if rule.status not in shadowable_statuses:
            continue
        if pattern.detection_type != DetectionType.TEXT_PATTERN:
            continue
        needle = pattern.search_pattern.strip()
        if not needle:
            continue
        for path in _iter_shadow_files(root, pattern.file_glob):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative_path = path.relative_to(root).as_posix()
            for index, line in enumerate(text.splitlines(), start=1):
                if needle not in line:
                    continue
                matches.append(
                    ShadowRuleMatch(
                        rule_id=rule.rule_id,
                        path=relative_path,
                        line=index,
                        snippet=line.strip(),
                        status=rule.status,
                    )
                )
    return matches


def discover_active_rule_findings(
    rules: Iterable[EmergentRule],
    worktree: Path,
    *,
    repo_name: str,
) -> List[Finding]:
    """Convert active emergent rule matches into proper Finding objects.

    Args:
        rules: Emergent rules (only ACTIVE ones are considered).
        worktree: Repository root to scan.
        repo_name: Repository identifier for the produced findings.

    Returns:
        List of Finding instances, one per active-rule match.
    """
    active_rules = [rule for rule in rules if rule.status == EmergentRuleStatus.ACTIVE]
    findings: List[Finding] = []
    for match in scan_shadow_rules(active_rules, worktree):
        rule = next(
            (
                candidate
                for candidate in active_rules
                if candidate.rule_id == match.rule_id
            ),
            None,
        )
        if rule is None:
            continue
        findings.append(
            Finding(
                finding_id=_emergent_finding_id(repo_name, match),
                repo=repo_name,
                path=match.path,
                line=match.line,
                rule=f"emergent:{rule.rule_id}",
                snippet=match.snippet,
                confidence=rule.confidence or 0.5,
                quick_win=False,
                safe_to_autofix=False,
                discovered_at=now_iso(),
                severity=rule.severity_default,
                category=rule.category,
            )
        )
    return findings


def _iter_shadow_files(root: Path, file_glob: str) -> List[Path]:
    globs = [file_glob or "**/*"]
    if file_glob.endswith("/**"):
        globs.append(f"{file_glob}/*")
    files: dict[str, Path] = {}
    for glob_pattern in globs:
        for path in root.glob(glob_pattern):
            if path.is_file():
                files[path.as_posix()] = path
    return [files[key] for key in sorted(files)]


def propose_rules_from_findings(
    findings: List[Finding],
    *,
    run_id: str,
    min_observations: int = 5,
    existing_rule_ids: set[str] | None = None,
) -> List[EmergentRule]:
    """Group findings by rule + directory and propose emergent rules for repeated patterns.

    Args:
        findings: Findings from a scan run.
        run_id: Identifier of the originating run.
        min_observations: Minimum group size to produce a proposal.
        existing_rule_ids: Rule IDs already covered; matching findings are
            excluded from grouping.

    Returns:
        List of proposed EmergentRule instances (status is PROPOSED).
    """
    existing_rule_ids = existing_rule_ids or set()
    groups: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        if finding.rule in existing_rule_ids:
            continue
        groups[(finding.rule, _directory_glob(finding.path))].append(finding)

    now = now_iso()
    proposals: List[EmergentRule] = []
    for (rule, file_glob), grouped in sorted(groups.items()):
        if len(grouped) < min_observations:
            continue
        finding_ids = [finding.finding_id for finding in grouped]
        proposals.append(
            EmergentRule(
                rule_id=_emergent_rule_id(rule, file_glob),
                header=f"Repeated {rule} findings in {file_glob}",
                detection_pattern=DetectionPattern(
                    detection_type=DetectionType.TEXT_PATTERN,
                    search_pattern=rule,
                    file_glob=file_glob,
                ),
                language=infer_language_from_path(grouped[0].path),
                category=_infer_category(rule),
                status=EmergentRuleStatus.PROPOSED,
                evidence_runs=[run_id],
                source_finding_ids=finding_ids,
                observation_count=len(grouped),
                confidence=min(0.95, 0.4 + (len(grouped) * 0.05)),
                created_at=now,
                updated_at=now,
                notes="Proposed from repeated findings; shadow validation required before activation.",
            )
        )
    return proposals


def propose_rules_from_fix_patterns(
    patterns: Iterable[Any],
    *,
    run_id: str,
    min_success_count: int = 5,
) -> List[EmergentRule]:
    """Group successful fix patterns by rule + directory and propose emergent rules.

    Args:
        patterns: Objects with ``success_count``, ``confidence``, ``rule``,
            and ``file_path`` attributes.
        run_id: Identifier of the originating run.
        min_success_count: Minimum successful applications required.

    Returns:
        List of proposed EmergentRule instances (status is PROPOSED).
    """
    groups: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for pattern in patterns:
        if int(getattr(pattern, "success_count", 0) or 0) < min_success_count:
            continue
        if float(getattr(pattern, "confidence", 0.0) or 0.0) < 0.5:
            continue
        groups[
            (
                str(getattr(pattern, "rule", "")),
                _directory_glob(str(getattr(pattern, "file_path", ""))),
            )
        ].append(pattern)

    now = now_iso()
    proposals: List[EmergentRule] = []
    for (rule, file_glob), grouped in sorted(groups.items()):
        source_finding_ids: list[str] = []
        observation_count = 0
        for pattern in grouped:
            observation_count += int(getattr(pattern, "success_count", 0) or 0)
            for finding_id in getattr(pattern, "source_finding_ids", []) or []:
                if finding_id and finding_id not in source_finding_ids:
                    source_finding_ids.append(finding_id)
        proposals.append(
            EmergentRule(
                rule_id=_emergent_rule_id(f"fix-pattern:{rule}", file_glob),
                header=f"Repeated successful {rule} fix patterns in {file_glob}",
                detection_pattern=DetectionPattern(
                    detection_type=DetectionType.TEXT_PATTERN,
                    search_pattern=rule,
                    file_glob=file_glob,
                ),
                language=str(getattr(grouped[0], "language", "all") or "all"),
                category=_infer_category(rule),
                status=EmergentRuleStatus.PROPOSED,
                evidence_runs=[run_id],
                source_finding_ids=source_finding_ids,
                observation_count=observation_count,
                confidence=min(0.95, 0.45 + (observation_count * 0.04)),
                created_at=now,
                updated_at=now,
                notes="Proposed from repeated successful fix patterns; shadow validation required.",
            )
        )
    return proposals


def _merge_observation(
    existing: EmergentRule, incoming: EmergentRule, run_id: str
) -> None:
    """Merge an incoming proposal's evidence and counts into an existing rule."""
    existing.observation_count += incoming.observation_count
    for evidence_run in incoming.evidence_runs or [run_id]:
        if evidence_run not in existing.evidence_runs:
            existing.evidence_runs.append(evidence_run)
    for finding_id in incoming.source_finding_ids:
        if finding_id not in existing.source_finding_ids:
            existing.source_finding_ids.append(finding_id)
    existing.confidence = min(0.95, 0.4 + (existing.observation_count * 0.05))
    existing.updated_at = now_iso()


def _proposal_key(rule: str, file_glob: str) -> tuple[str, str]:
    return (rule.strip().lower(), file_glob.strip().lower())


def _emergent_rule_id(rule: str, file_glob: str) -> str:
    digest = hashlib.sha256(f"{rule}:{file_glob}".encode("utf-8")).hexdigest()[:12]
    return f"er-{digest}"


def _emergent_finding_id(repo_name: str, match: ShadowRuleMatch) -> str:
    digest = hashlib.sha256(
        f"{repo_name}:{match.rule_id}:{match.path}:{match.line}:{match.snippet}".encode(
            "utf-8"
        )
    ).hexdigest()[:12]
    return f"erf-{digest}"


def _directory_glob(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) <= 1:
        return "**/*"
    return "/".join(parts[:-1]) + "/**"


# NOTE: ``_infer_language(path)`` and ``_infer_category(rule)`` were previously
# defined locally here as duplicates of ``bluei.engine.report``. They have been
# consolidated: language-from-path now lives in
# ``bluei.engine.report.infer_language_from_path`` and category inference uses
# ``bluei.engine.report._infer_category`` (which is regex-based and richer than
# the old substring heuristic). Both are imported at the top of this module.
