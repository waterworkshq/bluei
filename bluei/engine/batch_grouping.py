"""Batch finding grouping logic — pure functions for cluster analysis.

Extracted from bluei/engine/batch_pr.py during god-module decomposition
(branch refactor/god-batch-pr-decomp, phase 1). These functions take
finding lists + batch rules and produce grouping decisions; they have
no subprocess or worktree side effects.

Public API (re-exported from bluei.engine.batch_pr for backward compat):
    load_batch_rules, rule_matches, is_isolated, check_batch_conflicts,
    chunk_findings, group_findings_for_batch

Internal helpers also used by batch_recovery:
    _find_issue_for_finding, _severity_batch_cap, SEVERITY_ORDER
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from bluei.engine.models import BatchGroup, BatchRule, Finding


def load_batch_rules(rules_path: Path) -> List[BatchRule]:
    """Load batch rules from a YAML file.

    Returns a list of BatchRule objects.
    Raises FileNotFoundError if the file does not exist.
    """
    text = rules_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not data or "rules" not in data:
        return []

    rules: List[BatchRule] = []
    for entry in data["rules"]:
        rules.append(
            BatchRule(
                rule_pattern=str(entry.get("rule_pattern", "")),
                enabled=bool(entry.get("enabled", True)),
                group_by=str(entry.get("group_by", "rule")),
                max_batch_size=int(entry.get("max_batch_size", 20)),
                max_files_per_batch=int(entry.get("max_files_per_batch", 15)),
                max_loc_per_batch=int(entry.get("max_loc_per_batch", 500)),
                isolation=dict(entry.get("isolation", {})),
                priority=int(entry.get("priority", 99)),
                severity=str(entry.get("severity", "normal")),
            )
        )
    return rules


def rule_matches(finding_rule: str, rule_pattern: str) -> bool:
    """Match a finding's rule against a batch rule pattern.

    Supports:
    - Exact match: "ruff-c408" == "ruff-c408"
    - Glob/prefix match: "ruff-b904" matches "ruff-*"
    """
    if "*" in rule_pattern:
        return fnmatch.fnmatch(finding_rule, rule_pattern)
    return finding_rule == rule_pattern


def is_isolated(finding: Finding, isolation_config: dict) -> bool:
    """Check if a finding should be excluded from batching.

    Reasons for isolation:
    - File matches an isolation file_pattern (e.g., migrations, middleware)
    """
    if not isolation_config:
        return False

    file_patterns = isolation_config.get("file_patterns", [])
    for pattern in file_patterns:
        if fnmatch.fnmatch(finding.path, pattern):
            return True
    return False


def check_batch_conflicts(findings: List[Finding]) -> List[Tuple[Finding, Finding]]:
    """Detect potential conflicts within a batch.

    Two findings conflict if:
    - They're in the same file AND
    - Their line numbers are within 5 lines of each other

    Returns a list of conflicting (finding_a, finding_b) pairs.
    """
    by_file: Dict[str, List[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)

    conflicts: List[Tuple[Finding, Finding]] = []
    for path, file_findings in by_file.items():
        sorted_findings = sorted(file_findings, key=lambda f: f.line)
        for i in range(len(sorted_findings) - 1):
            if sorted_findings[i + 1].line - sorted_findings[i].line < 5:
                conflicts.append((sorted_findings[i], sorted_findings[i + 1]))

    return conflicts


def chunk_findings(
    findings: List[Finding], rule_config: BatchRule
) -> List[List[Finding]]:
    """Split findings into chunks respecting size limits.

    Respects:
    - max_batch_size: max findings per chunk
    - max_files_per_batch: max unique files per chunk
    - max_loc_per_batch: estimated max lines changed (soft cap)

    Strategy: greedy fill — add findings to current chunk until a limit
    is hit, then start a new chunk.
    """
    if not findings:
        return []

    # Dynamic batch sizing: auto-determine max_batch_size based on file density.
    # Rationale: if 20 findings touch 5 files, they can be one batch (small PR).
    # If 20 findings touch 20 files, split into smaller batches.
    unique_files = len(set(f.path for f in findings))
    files_per_finding = unique_files / len(findings) if findings else 1.0

    # Target: keep each batch to a manageable number of unique files
    target_files = rule_config.max_files_per_batch
    auto_batch_size = max(3, min(30, int(target_files / files_per_finding)))

    # Use the SMALLER of auto_batch_size and configured max_batch_size
    effective_max_batch = min(auto_batch_size, rule_config.max_batch_size)

    chunks: List[List[Finding]] = []
    current: List[Finding] = []
    current_files: set = set()
    current_est_loc = 0

    for finding in findings:
        file_in_chunk = finding.path not in current_files

        if len(current) >= effective_max_batch or (
            file_in_chunk and len(current_files) >= rule_config.max_files_per_batch
        ):
            chunks.append(current)
            current = []
            current_files = set()
            current_est_loc = 0

        current.append(finding)
        current_files.add(finding.path)
        current_est_loc += 1  # estimate: 1 line per micro fix

        # Soft check for LOC — only split if we already have findings
        if current_est_loc >= rule_config.max_loc_per_batch and len(current) > 1:
            chunks.append(current)
            current = []
            current_files = set()
            current_est_loc = 0

    if current:
        chunks.append(current)

    return chunks


def _severity_batch_cap(severity: str, rule_max: int) -> int:
    """Return effective max_batch_size based on severity level.

    - critical → 1 (always solo)
    - high → min(rule_max, 5)
    - normal → rule_max
    - low → min(max(rule_max, 30), 30)  # allow up to 30
    """
    if severity == "critical":
        return 1
    if severity == "high":
        return min(rule_max, 5)
    if severity == "low":
        return 30
    return rule_max  # normal


SEVERITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3}


def group_findings_for_batch(
    queue_candidates: List[Tuple[dict, Finding]],
    batch_rules: List[BatchRule],
) -> List[BatchGroup]:
    """Main grouping entry point.

    Input: list of (issue_dict, Finding) tuples from pr-cycle queue
    Output: list of BatchGroup objects (some solo, some multi-finding)

    Algorithm:
    1. Sort batch rules by priority
    2. For each rule, find matching findings
    3. Handle cross-rule grouping (group_by="cross-rule")
    4. Apply severity-based caps (critical=solo, high=max5, low=max30)
    5. Separate isolated findings (always solo)
    6. Chunk remaining findings into batches
    7. Sort resulting batches by severity order
    8. Any ungrouped findings become solo batches
    """
    batches: List[BatchGroup] = []
    batched_ids: set = set()

    # Filter to enabled rules, sort by priority
    enabled_rules = sorted(
        [r for r in batch_rules if r.enabled],
        key=lambda r: r.priority,
    )

    for rule_config in enabled_rules:
        # Cross-rule: collect findings matching wildcard across all specific rules
        if rule_config.group_by == "cross-rule":
            matching = [
                (issue, f)
                for issue, f in queue_candidates
                if rule_matches(f.rule, rule_config.rule_pattern)
                and f.finding_id not in batched_ids
            ]
            if not matching:
                continue

            # Separate isolated from batchable
            isolated: List[Tuple[dict, Finding]] = []
            batchable: List[Tuple[dict, Finding]] = []
            for issue, f in matching:
                if is_isolated(f, rule_config.isolation):
                    isolated.append((issue, f))
                else:
                    batchable.append((issue, f))

            # Create solo batches for isolated findings
            for issue, f in isolated:
                batches.append(BatchGroup.from_solo(issue, f))
                batched_ids.add(f.finding_id)

            # Chunk and create cross-rule batches
            findings_only = [f for _, f in batchable]
            if findings_only:
                # Apply severity cap
                effective_cap = _severity_batch_cap(
                    rule_config.severity, rule_config.max_batch_size
                )
                capped_config = BatchRule(
                    rule_pattern=rule_config.rule_pattern,
                    group_by=rule_config.group_by,
                    max_batch_size=effective_cap,
                    max_files_per_batch=rule_config.max_files_per_batch,
                    max_loc_per_batch=rule_config.max_loc_per_batch,
                )
                for chunk in chunk_findings(findings_only, capped_config):
                    issues_map = {f.finding_id: issue for issue, f in batchable}
                    group = BatchGroup.from_findings(chunk, issues_map, capped_config)
                    # Override rule_pattern to the wildcard pattern
                    group.rule_pattern = rule_config.rule_pattern
                    batches.append(group)
                    batched_ids.update(f.finding_id for f in chunk)
            continue

        # Standard per-rule grouping
        matching = [
            (issue, f)
            for issue, f in queue_candidates
            if rule_matches(f.rule, rule_config.rule_pattern)
            and f.finding_id not in batched_ids
        ]

        if not matching:
            continue

        # Severity: critical findings → always solo
        if rule_config.severity == "critical":
            for issue, f in matching:
                batches.append(BatchGroup.from_solo(issue, f))
                batched_ids.add(f.finding_id)
            continue

        # Separate isolated from batchable
        isolated: List[Tuple[dict, Finding]] = []
        batchable: List[Tuple[dict, Finding]] = []
        for issue, f in matching:
            if is_isolated(f, rule_config.isolation):
                isolated.append((issue, f))
            else:
                batchable.append((issue, f))

        # Create solo batches for isolated findings
        for issue, f in isolated:
            batches.append(BatchGroup.from_solo(issue, f))
            batched_ids.add(f.finding_id)

        # Chunk and create multi-finding batches with severity cap
        findings_only = [f for _, f in batchable]
        if findings_only:
            effective_cap = _severity_batch_cap(
                rule_config.severity, rule_config.max_batch_size
            )
            capped_config = BatchRule(
                rule_pattern=rule_config.rule_pattern,
                group_by=rule_config.group_by,
                max_batch_size=effective_cap,
                max_files_per_batch=rule_config.max_files_per_batch,
                max_loc_per_batch=rule_config.max_loc_per_batch,
                isolation=rule_config.isolation,
                priority=rule_config.priority,
                severity=rule_config.severity,
            )
            for chunk in chunk_findings(findings_only, capped_config):
                issues_map = {f.finding_id: issue for issue, f in batchable}
                group = BatchGroup.from_findings(chunk, issues_map, capped_config)
                batches.append(group)
                batched_ids.update(f.finding_id for f in chunk)

    # Any remaining findings become solo
    for issue, f in queue_candidates:
        if f.finding_id not in batched_ids:
            batches.append(BatchGroup.from_solo(issue, f))

    # Sort batches by severity order (critical first → low last)
    def _batch_severity_sort_key(bg: BatchGroup) -> int:
        # Solo batches get highest priority (0 = critical tier)
        if bg.is_solo:
            return 0
        # Find the rule config that produced this batch to get severity
        for r in enabled_rules:
            if r.rule_pattern == bg.rule_pattern:
                return SEVERITY_ORDER.get(r.severity, 2)
        # Fallback: try matching by finding rules
        for r in enabled_rules:
            if any(rule_matches(f.rule, r.rule_pattern) for f in bg.findings):
                return SEVERITY_ORDER.get(r.severity, 2)
        return 2  # default normal

    batches.sort(key=_batch_severity_sort_key)

    return batches


def _find_issue_for_finding(issues: list, finding_id: str) -> Optional[dict]:
    """Look up the issue dict corresponding to a finding_id."""
    for issue in issues:
        fid = issue.get("finding_id") or issue.get("id")
        if fid == finding_id:
            return issue
    return None
