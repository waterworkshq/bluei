"""Data structures and planning logic for campaign orchestration.

Provides Campaign and CampaignPhase dataclasses, a CampaignPlanner that groups
findings into ordered phases by rule type or file, and helper functions for
rule priority classification.
"""

from __future__ import annotations

import logging
import fnmatch
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import (
    CampaignStatus,
    CampaignStrategy,
    LearningObjective,
    PhaseStatus,
    PhaseExecutionMode,
)
from bluei.engine.models import Finding

_logger = logging.getLogger(__name__)


RULE_PRIORITY = {
    # Numeric priority for each finding category; lower values run first.
    "bug": 0,
    "type": 1,
    "test": 2,
    "refactor": 3,
    "lint": 4,
    "docs": 5,
}

BUG_RULES = {
    # Rules classified as bugs (highest priority, always phase 0).
    "discount-math-sign",
    "catalog-query-not-normalized",
}


def _now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


@dataclass
class CampaignPhase:
    """A single execution phase within a campaign, holding one or more findings."""

    phase_id: str
    index: int
    title: str
    finding_ids: List[str]
    execution_mode: str = PhaseExecutionMode.parallel
    max_parallel: int = 3
    depends_on: List[str] = field(default_factory=list)
    status: str = PhaseStatus.pending
    findings_fixed: int = 0
    findings_failed: int = 0
    findings_skipped: int = 0
    pre_phase_checks: List[List[str]] = field(default_factory=list)
    post_phase_checks: List[List[str]] = field(default_factory=list)
    integration_checks: List[List[str]] = field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize this phase to a JSON-compatible dict.

        Returns:
            Dict with all phase fields.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CampaignPhase":
        """Reconstruct a CampaignPhase from a persisted dict.

        Args:
            data: Dict produced by to_dict().

        Returns:
            CampaignPhase instance.
        """
        payload = dict(data)
        payload.setdefault("findings_skipped", 0)
        return cls(**payload)


@dataclass
class Campaign:
    """Top-level campaign object holding phases, findings, and execution metadata."""

    campaign_id: str
    repo: str
    title: str
    description: str
    strategy: str
    target_rules: List[str]
    target_paths: List[str]
    target_findings: List[str]
    finding_snapshots: Dict[str, Dict[str, Any]]
    phases: List[CampaignPhase]
    dependency_graph: Dict[str, List[str]]
    status: str = CampaignStatus.planning
    current_phase_index: int = 0
    total_findings: int = 0
    findings_fixed: int = 0
    findings_failed: int = 0
    findings_skipped: int = 0
    max_phases: int = 10
    max_findings_total: int = 50
    max_files_total: int = 30
    requires_clean_baseline: bool = True
    validation_between_phases: bool = True
    estimated_llm_calls: int = 0
    actual_llm_calls: int = 0
    abort_reason: Optional[str] = None
    pause_reason: Optional[str] = None
    pause_phase_id: Optional[str] = None
    pause_finding_id: Optional[str] = None
    paused_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    learning_objective: Optional[LearningObjective] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the campaign including nested phases.

        Returns:
            JSON-compatible dict with all campaign fields.
        """
        data = asdict(self)
        data["phases"] = [phase.to_dict() for phase in self.phases]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Campaign":
        """Reconstruct a Campaign from a persisted dict, including nested phases.

        Args:
            data: Dict produced by to_dict().

        Returns:
            Campaign instance with populated phases.
        """
        payload = dict(data)
        payload.setdefault("finding_snapshots", {})
        payload.setdefault("pause_reason", None)
        payload.setdefault("pause_phase_id", None)
        payload.setdefault("pause_finding_id", None)
        payload.setdefault("paused_at", None)
        payload["phases"] = [
            CampaignPhase.from_dict(phase) for phase in payload.get("phases", [])
        ]
        objective = payload.get("learning_objective")
        payload["learning_objective"] = (
            LearningObjective.from_dict(objective) if objective else None
        )
        return cls(**payload)


class CampaignPlanner:
    """Create a dry-run campaign plan from already-discovered findings.

    Supports three strategies: rule_based (group by rule priority),
    depth_first (group by file), and dependency_ordered (topological file order).
    """

    def __init__(
        self,
        *,
        max_phases: int = 10,
        max_findings_total: int = 50,
        max_files_total: int = 30,
    ) -> None:
        """
        Args:
            max_phases: Abort if the plan would exceed this many phases.
            max_findings_total: Abort if more findings than this are supplied.
            max_files_total: Abort if findings span more unique files than this.
        """
        self.max_phases = max_phases
        self.max_findings_total = max_findings_total
        self.max_files_total = max_files_total

    def plan(
        self,
        findings: List[Finding],
        *,
        repo: str,
        title: str,
        target_rules: Optional[List[str]] = None,
        target_paths: Optional[List[str]] = None,
        strategy: str = CampaignStrategy.rule_based,
        learning_objective: Optional[LearningObjective] = None,
    ) -> Campaign:
        """Build a Campaign with ordered phases from a list of findings.

        Args:
            findings: Raw findings to group into phases.
            repo: Repository identifier.
            title: Human-readable campaign title.
            target_rules: Only include findings matching these rule names.
            target_paths: Only include findings whose paths match these glob patterns.
            strategy: Phasing strategy (rule_based, depth_first, dependency_ordered).

        Returns:
            Campaign object with populated phases, or an aborted campaign if
            safety limits are exceeded.
        """
        filtered = self._filter_findings(findings, target_rules, target_paths)
        campaign = self._base_campaign(
            repo=repo,
            title=title,
            target_rules=target_rules or [],
            target_paths=target_paths or [],
            target_findings=[finding.finding_id for finding in filtered],
            finding_snapshots={
                finding.finding_id: finding.to_dict() for finding in filtered
            },
            strategy=strategy,
        )
        campaign.total_findings = len(filtered)
        campaign.estimated_llm_calls = sum(
            1 for finding in filtered if not finding.safe_to_autofix
        )
        campaign.learning_objective = learning_objective

        abort_reason = self._safety_abort_reason(filtered)
        if abort_reason is not None:
            campaign.status = CampaignStatus.aborted
            campaign.abort_reason = abort_reason
            return campaign

        if strategy == CampaignStrategy.depth_first:
            campaign.phases = self._build_phases_depth_first(
                campaign.campaign_id, filtered
            )
        elif strategy == CampaignStrategy.dependency_ordered:
            campaign.phases = self._build_phases_dependency_ordered(
                campaign.campaign_id, filtered
            )
        else:
            campaign.phases = self._build_phases(campaign.campaign_id, filtered)
        campaign.dependency_graph = {
            phase.phase_id: list(phase.depends_on) for phase in campaign.phases
        }
        if len(campaign.phases) > self.max_phases:
            campaign.status = CampaignStatus.aborted
            campaign.abort_reason = "max_phases exceeded"
        return campaign

    def _base_campaign(
        self,
        *,
        repo: str,
        title: str,
        target_rules: List[str],
        target_paths: List[str],
        target_findings: List[str],
        finding_snapshots: Dict[str, Dict[str, Any]],
        strategy: str,
    ) -> Campaign:
        now = datetime.now(timezone.utc).isoformat()
        return Campaign(
            campaign_id=f"camp-{_now_compact()}-{uuid.uuid4().hex[:8]}",
            repo=repo,
            title=title,
            description=f"Dry-run plan for {len(target_findings)} finding(s)",
            strategy=strategy,
            target_rules=list(target_rules),
            target_paths=list(target_paths),
            target_findings=list(target_findings),
            finding_snapshots=dict(finding_snapshots),
            phases=[],
            dependency_graph={},
            max_phases=self.max_phases,
            max_findings_total=self.max_findings_total,
            max_files_total=self.max_files_total,
            created_at=now,
            updated_at=now,
        )

    def _filter_findings(
        self,
        findings: List[Finding],
        target_rules: Optional[List[str]],
        target_paths: Optional[List[str]],
    ) -> List[Finding]:
        """Keep only findings matching target rules and path globs.

        Args:
            findings: Unfiltered findings.
            target_rules: Rule names to keep (None = keep all).
            target_paths: Glob patterns to match against finding paths (None = keep all).

        Returns:
            Filtered list of findings.
        """
        rules = {rule for rule in (target_rules or []) if rule}
        paths = [path for path in (target_paths or []) if path]

        result: List[Finding] = []
        for finding in findings:
            if rules and finding.rule not in rules:
                continue
            if paths and not any(
                fnmatch.fnmatch(finding.path, pattern) for pattern in paths
            ):
                continue
            result.append(finding)
        return result

    def _safety_abort_reason(self, findings: List[Finding]) -> Optional[str]:
        """Return a human-readable abort reason if safety limits are exceeded, else None.

        Args:
            findings: Filtered findings to check.

        Returns:
            Abort reason string, or None if all checks pass.
        """
        if len(findings) > self.max_findings_total:
            return "max_findings_total exceeded"
        if len({finding.path for finding in findings}) > self.max_files_total:
            return "max_files_total exceeded"
        return None

    def _build_phases(
        self,
        campaign_id: str,
        findings: List[Finding],
    ) -> List[CampaignPhase]:
        """Group findings into phases by rule priority (rule_based strategy).

        Findings needing file-serial execution (type, refactor) each get a
        dedicated phase; others share a phase per rule.

        Args:
            campaign_id: Parent campaign ID for phase ID generation.
            findings: Findings to partition.

        Returns:
            Ordered list of CampaignPhase objects.
        """
        phases: List[CampaignPhase] = []
        previous_phase_id: Optional[str] = None

        grouped: Dict[tuple[int, str, str], List[Finding]] = {}
        for finding in findings:
            priority = _rule_priority(finding.rule)
            file_key = finding.path if _needs_file_serial_phase(finding.rule) else ""
            grouped.setdefault((priority, finding.rule, file_key), []).append(finding)

        for index, ((_, rule, file_key), group) in enumerate(
            sorted(grouped.items()), start=1
        ):
            phase_id = f"phase-{campaign_id[5:13]}-{index - 1}"
            title = f"Fix {rule} findings"
            if file_key:
                title = f"{title} in {file_key}"
            depends_on = [previous_phase_id] if previous_phase_id else []
            phase = CampaignPhase(
                phase_id=phase_id,
                index=index - 1,
                title=title,
                finding_ids=[
                    finding.finding_id
                    for finding in sorted(group, key=lambda f: f.line)
                ],
                execution_mode=PhaseExecutionMode.sequential
                if file_key
                else PhaseExecutionMode.parallel,
                depends_on=depends_on,
            )
            phases.append(phase)
            previous_phase_id = phase_id

        return phases

    def _build_phases_depth_first(
        self,
        campaign_id: str,
        findings: List[Finding],
    ) -> List[CampaignPhase]:
        """Group findings into one phase per file, sorted by priority within each file.

        Args:
            campaign_id: Parent campaign ID for phase ID generation.
            findings: Findings to partition.

        Returns:
            Ordered list of CampaignPhase objects.
        """
        grouped: Dict[str, List[Finding]] = {}
        for finding in findings:
            grouped.setdefault(finding.path, []).append(finding)
        phases: List[CampaignPhase] = []
        previous_phase_id: Optional[str] = None
        for index, (file_path, group) in enumerate(sorted(grouped.items())):
            phase_id = f"phase-{campaign_id[5:13]}-{index}"
            sorted_group = sorted(group, key=lambda f: (_rule_priority(f.rule), f.line))
            phase = CampaignPhase(
                phase_id=phase_id,
                index=index,
                title=f"Fix all findings in {file_path}",
                finding_ids=[finding.finding_id for finding in sorted_group],
                execution_mode=PhaseExecutionMode.sequential,
                depends_on=[previous_phase_id] if previous_phase_id else [],
            )
            phases.append(phase)
            previous_phase_id = phase_id
        return phases

    def _build_phases_dependency_ordered(
        self,
        campaign_id: str,
        findings: List[Finding],
    ) -> List[CampaignPhase]:
        """Group findings by file, ordered by import-graph topological sort.

        Falls back to depth_first if the import graph cannot be built.

        Args:
            campaign_id: Parent campaign ID for phase ID generation.
            findings: Findings to partition.

        Returns:
            Ordered list of CampaignPhase objects.
        """
        from bluei.app.import_graph import build_import_graph

        repo_path = getattr(findings[0], "repo", None) if findings else None
        if not repo_path:
            return self._build_phases_depth_first(campaign_id, findings)
        try:
            graph = build_import_graph(Path(repo_path))
            ordered = graph.topological_sort()
        except Exception:
            return self._build_phases_depth_first(campaign_id, findings)
        file_order = {f: i for i, f in enumerate(ordered)}
        grouped: Dict[str, List[Finding]] = {}
        for finding in findings:
            grouped.setdefault(finding.path, []).append(finding)
        sorted_groups = sorted(
            grouped.items(), key=lambda x: file_order.get(x[0], 999999)
        )
        phases: List[CampaignPhase] = []
        previous_phase_id: Optional[str] = None
        for index, (file_path, group) in enumerate(sorted_groups):
            phase_id = f"phase-{campaign_id[5:13]}-{index}"
            sorted_group = sorted(group, key=lambda f: (_rule_priority(f.rule), f.line))
            phase = CampaignPhase(
                phase_id=phase_id,
                index=index,
                title=f"Fix all findings in {file_path} (dep-ordered)",
                finding_ids=[finding.finding_id for finding in sorted_group],
                execution_mode=PhaseExecutionMode.sequential,
                depends_on=[previous_phase_id] if previous_phase_id else [],
            )
            phases.append(phase)
            previous_phase_id = phase_id
        return phases


def _rule_priority(rule: str) -> int:
    """Map a rule name to its numeric priority (lower = runs earlier).

    Args:
        rule: Rule name string from a Finding.

    Returns:
        Integer priority from RULE_PRIORITY.
    """
    if rule in BUG_RULES:
        return RULE_PRIORITY["bug"]
    if rule.startswith("type-") or "type" in rule:
        return RULE_PRIORITY["type"]
    if rule.startswith("test-"):
        return RULE_PRIORITY["test"]
    if rule.startswith("xo-") or "complexity" in rule or "max-lines" in rule:
        return RULE_PRIORITY["refactor"]
    if rule.startswith("docs-"):
        return RULE_PRIORITY["docs"]
    return RULE_PRIORITY["lint"]


def _needs_file_serial_phase(rule: str) -> bool:
    """Return True if findings for this rule must be serialized per file.

    Args:
        rule: Rule name string from a Finding.

    Returns:
        True if type or refactor priority.
    """
    return _rule_priority(rule) in {
        RULE_PRIORITY["type"],
        RULE_PRIORITY["refactor"],
    }
