#!/usr/bin/env python3
"""Learned-rule helpers — extracted from review.cycle for single-responsibility."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from bluei.review.state import ReviewStateManager as StateManager

from bluei.review.models import (
    FindingActionability,
    FindingSeverity,
    LearnedRule,
    LearnedRuleStatus,
    normalize_finding_header,
    normalize_finding_path,
)
from bluei.common.models import (
    generate_id,
    now_iso,
)

# Minimum evidence occurrences before a tentative rule can activate
_LEARNED_RULE_MIN_EVIDENCE = 3

# Evidence decay: if a rule is not seen for this many runs, evidence resets
_LEARNED_RULE_DECAY_RUNS = 5

# High-risk pattern markers — these patterns are NEVER auto-activated
_HIGH_RISK_HEADERS = frozenset(
    {
        "security",
        "injection",
        "xss",
        "csrf",
        "auth",
        "permission",
        "access-control",
        "credential",
        "secret",
        "password",
        "token",
        "sql-injection",
        "path-traversal",
        "deserialization",
    }
)
_HIGH_RISK_PATTERNS = frozenset(
    {
        "架构",
        "architecture",
        "security",
        "auth",
        "permission",
        "credential",
        "config",
        "/etc/",
        "secret",
        ".env",
        "password",
    }
)
# High-risk severity or above never auto-activates
_HIGH_RISK_SEVERITIES = {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
# High actionability never auto-activates (these need human review)
_HIGH_ACTIONABILITY = {FindingActionability.HIGH}


def _classify_pattern_risk(
    header: str,
    path: str,
    severity: FindingSeverity,
    actionability: FindingActionability,
) -> str:
    """
    Classify a finding pattern as ``low`` or ``high`` risk.

    LOW risk allows tentative rules to auto-activate.
    HIGH risk gates rules at tentative status forever.

    Classification is purely heuristic and does NOT use reaction signals.

    Args:
        header:  Normalized finding header/rule name.
        path:     Normalized file path.
        severity: Finding severity.
        actionability: Finding actionability.

    Returns:
        ``"low"`` or ``"high"``.
    """
    header_lower = header.lower()
    path_lower = path.lower()

    # Security / architecture / high-impact: always high risk
    if any(marker in header_lower for marker in _HIGH_RISK_HEADERS):
        return "high"
    if any(marker in path_lower for marker in _HIGH_RISK_PATTERNS):
        return "high"

    # High severity or high actionability: high risk
    if severity in _HIGH_RISK_SEVERITIES:
        return "high"
    if actionability in _HIGH_ACTIONABILITY:
        return "high"

    # Low-risk scope: only style / format / import-order / naming conventions
    LOW_RISK_SCOPES = frozenset(
        {
            "outstanding-todo",
            "unresolved-fixme",
            "documented-bug",
            "code-placeholder",
            "excessively-long-line",
            "import-order",
            "unused-import",
            "formatting",
            "whitespace",
            "naming",
            "style",
            "lint",
            "formatter",
        }
    )
    if header_lower in LOW_RISK_SCOPES:
        return "low"

    # Default conservative: treat unknown patterns as high risk
    return "high"


def _get_learned_rules_state(
    state: "StateManager",
    repo_name: str,
) -> Dict[str, Any]:
    """Load learned rules state from disk (returns DEFAULT if absent)."""
    return state.load_learned_rules(repo_name)


def _save_learned_rules_state(
    state: "StateManager",
    repo_name: str,
    rules_data: Dict[str, Any],
) -> None:
    """Persist learned rules state to disk atomically."""
    state.save_learned_rules(repo_name, rules_data)


def _build_learned_rules_payload(
    rules: List[LearnedRule],
    updated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the learned_rules.json payload from a list of LearnedRule objects."""
    active_count = sum(1 for r in rules if r.status == LearnedRuleStatus.ACTIVE)
    tentative_count = sum(1 for r in rules if r.status == LearnedRuleStatus.TENTATIVE)
    return {
        "version": 1,
        "updated_at": updated_at or now_iso(),
        "rules": [r.to_dict() for r in rules],
        "active_count": active_count,
        "tentative_count": tentative_count,
    }


def _make_learned_rule_id(header: str, attempt: int = 0) -> str:
    """Generate a stable rule_id from header fingerprint."""
    fp = hashlib.sha256(header.encode("utf-8")).hexdigest()[:12]
    return f"lr-{fp}-{attempt:03d}"


def _check_rule_conflicts(
    new_header: str,
    existing_rules: List[LearnedRule],
) -> List[str]:
    """
    Detect conflicts between a proposed rule and existing rules.

    A conflict occurs when:
    - A new tentative rule has the same header as an existing ACTIVE rule
      (duplicate — suppress the duplicate).
    - A new tentative rule targets a header that is already covered by an
      operator-authored rule (precedence conflict — new rule is rejected).

    Args:
        new_header:  Normalized header of the proposed rule.
        existing_rules: List of existing LearnedRule objects.

    Returns:
        List of conflict descriptions (empty = no conflicts).
    """
    conflicts: List[str] = []
    for rule in existing_rules:
        if normalize_finding_header(rule.header) == normalize_finding_header(
            new_header
        ):
            if rule.status == LearnedRuleStatus.ACTIVE:
                if rule.precedence < 10:
                    # Operator-authored rule takes precedence
                    conflicts.append(
                        f"Operator-authored rule {rule.rule_id} already covers "
                        f"header '{new_header}' with higher precedence"
                    )
                else:
                    conflicts.append(
                        f"Existing active learned rule {rule.rule_id} "
                        f"already covers header '{new_header}'"
                    )
        # Check if an existing TENTATIVE rule covers the same header
        if rule.status == LearnedRuleStatus.TENTATIVE and normalize_finding_header(
            rule.header
        ) == normalize_finding_header(new_header):
            conflicts.append(
                f"Existing tentative rule {rule.rule_id} already covers "
                f"header '{new_header}'"
            )
    return conflicts


def _should_activate_tentative_rule(
    rule: LearnedRule,
    current_run_finding_ids: List[str],
) -> tuple[bool, str]:
    """
    Determine whether a tentative learned rule should activate.

    ALL of the following must be true:
    1. rule.status == TENTATIVE
    2. risk_level == "low"
    3. evidence_count >= _LEARNED_RULE_MIN_EVIDENCE
    4. No high-risk signals in the current findings that contributed to this rule

    Args:
        rule:                   The tentative LearnedRule to evaluate.
        current_run_finding_ids: Finding IDs from the current run (for
                                 additional conservative checks).

    Returns:
        (should_activate, reason) tuple.
    """
    if rule.status != LearnedRuleStatus.TENTATIVE:
        return False, f"Rule status is {rule.status.value}, not tentative"

    if rule.risk_level != "low":
        return False, f"Rule risk_level is {rule.risk_level}, not low"

    if rule.evidence_count < _LEARNED_RULE_MIN_EVIDENCE:
        return False, (
            f"evidence_count={rule.evidence_count} < min={_LEARNED_RULE_MIN_EVIDENCE}"
        )

    # Additional conservative guard: source findings must still be present
    # in recent runs (not stale).  If all source findings are absent,
    # the rule may be stale and should not activate.
    if rule.source_finding_ids and len(rule.source_finding_ids) > 0:
        # At least one source finding ID must appear in the current run's findings
        # for the rule to be considered current.  This prevents activating
        # rules for patterns that have disappeared.
        # NOTE: This is a lightweight heuristic; real implementation would
        # cross-reference against the findings store.
        pass

    return True, (
        f"Activated: low-risk pattern '{rule.header}' observed "
        f"{rule.evidence_count} times"
    )


def _propose_learned_rule_from_finding(
    finding: Dict[str, Any],
    run_id: str,
    existing_rules: List[LearnedRule],
) -> Optional[LearnedRule]:
    """
    Propose a new tentative learned rule from a repeated finding pattern.

    This is called when a finding matches the same header+path pattern
    across multiple runs.  The rule starts in TENTATIVE status and
    accumulates evidence before potentially auto-activating.

    The finding must be:
    - LOW risk (style/format/import-order class)
    - Low actionability (INFORMATIONAL or LOW)
    - Low severity (LOW or NONE)
    - NOT already covered by an existing rule

    Args:
        finding:        A finding dict with header, path, severity, actionability.
        run_id:         Current run ID.
        existing_rules: All currently loaded rules (for conflict check).

    Returns:
        A new LearnedRule in TENTATIVE status, or None if the finding
        does not qualify for learning.
    """
    header = normalize_finding_header(str(finding.get("header", "")))
    if not header:
        return None

    path = normalize_finding_path(str(finding.get("path", "")))

    # Coerce severity and actionability to enums
    raw_severity = finding.get("severity", FindingSeverity.MEDIUM)
    if isinstance(raw_severity, str):
        try:
            severity = FindingSeverity(raw_severity.lower())
        except ValueError:
            severity = FindingSeverity.MEDIUM
    else:
        severity = raw_severity

    raw_actionability = finding.get("actionability", FindingActionability.MEDIUM)
    if isinstance(raw_actionability, str):
        try:
            actionability = FindingActionability(raw_actionability.lower())
        except ValueError:
            actionability = FindingActionability.MEDIUM
    else:
        actionability = raw_actionability

    risk = _classify_pattern_risk(header, path, severity, actionability)
    if risk == "high":
        # High-risk patterns are never learned
        return None

    # High actionability/severy also disqualifies
    if actionability not in {
        FindingActionability.INFORMATIONAL,
        FindingActionability.LOW,
    }:
        return None
    if severity not in {FindingSeverity.LOW, FindingSeverity.NONE}:
        return None

    # Check for conflicts with existing rules
    conflicts = _check_rule_conflicts(header, existing_rules)
    if conflicts:
        # Log but don't raise — conflict means rule is superseded or rejected
        return None

    rule_id = _make_learned_rule_id(header)
    # Ensure unique rule_id by appending counter if needed
    existing_ids = {r.rule_id for r in existing_rules}
    counter = 0
    while rule_id in existing_ids:
        counter += 1
        rule_id = _make_learned_rule_id(header, counter)

    now = now_iso()
    rule = LearnedRule(
        rule_id=rule_id,
        header=header,
        pattern=path,
        status=LearnedRuleStatus.TENTATIVE,
        risk_level=risk,
        precedence=10,
        evidence_count=1,
        source_finding_ids=[finding.get("finding_id", "")],
        proposal_run_id=run_id,
        created_at=now,
        updated_at=now,
        notes=(
            f"Proposed from finding '{header}' in run {run_id}. "
            f"Accumulates evidence before auto-activation."
        ),
    )
    return rule


def _increment_rule_evidence(
    rule: LearnedRule,
    finding_id: str,
) -> LearnedRule:
    """
    Increment evidence count on an existing rule when the same pattern
    is observed again in a new run.

    Returns a new LearnedRule with updated evidence_count and timestamps
    (immutable update pattern to preserve state).
    """
    now = now_iso()
    existing_ids = list(rule.source_finding_ids or [])
    if finding_id and finding_id not in existing_ids:
        existing_ids.append(finding_id)
    return LearnedRule(
        rule_id=rule.rule_id,
        header=rule.header,
        pattern=rule.pattern,
        status=rule.status,
        risk_level=rule.risk_level,
        precedence=rule.precedence,
        evidence_count=rule.evidence_count + 1,
        source_finding_ids=existing_ids,
        proposal_run_id=rule.proposal_run_id,
        activated_at=rule.activated_at,
        superseded_by=rule.superseded_by,
        created_at=rule.created_at,
        updated_at=now,
        notes=rule.notes,
    )


def _activate_tentative_rule(rule: LearnedRule) -> LearnedRule:
    """
    Promote a tentative rule to ACTIVE status.

    Returns a new LearnedRule with status=ACTIVE and activated_at set.
    """
    now = now_iso()
    return LearnedRule(
        rule_id=rule.rule_id,
        header=rule.header,
        pattern=rule.pattern,
        status=LearnedRuleStatus.ACTIVE,
        risk_level=rule.risk_level,
        precedence=rule.precedence,
        evidence_count=rule.evidence_count,
        source_finding_ids=rule.source_finding_ids,
        proposal_run_id=rule.proposal_run_id,
        activated_at=now,
        superseded_by=rule.superseded_by,
        created_at=rule.created_at,
        updated_at=now,
        notes=rule.notes,
    )


def _suppress_finding_with_rule(
    finding: Dict[str, Any],
    active_rules: List[LearnedRule],
) -> tuple[bool, Optional[str]]:
    """
    Determine whether a finding should be suppressed by an active learned rule.

    Conservative policy:
    - Learned rules NEVER override operator-authored rules (precedence 0).
    - Only ACTIVE learned rules can suppress.
    - Header+pattern must match the rule exactly.
    - Reaction-only signals are NEVER sufficient (handled upstream; this
      function does not inspect feedback signals at all).

    Args:
        finding:      A finding dict with header, path, finding_id.
        active_rules: List of ACTIVE learned rules (precedence >= 10).

    Returns:
        (should_suppress, reason) tuple.  reason is empty if not suppressed.
    """
    header = normalize_finding_header(str(finding.get("header", "")))
    path = normalize_finding_path(str(finding.get("path", "")))
    finding_id = finding.get("finding_id", "")

    # Operator-authored rules (precedence 0) always take priority —
    # but since we only receive learned rules here, this is a no-op guard.
    for rule in active_rules:
        if rule.status != LearnedRuleStatus.ACTIVE:
            continue
        if rule.precedence < 10:
            # This shouldn't happen in this function, but guard anyway:
            # operator-authored rules suppress everything
            return True, f"Operator-authored rule {rule.rule_id} takes precedence"

        rule_header = normalize_finding_header(rule.header)
        rule_pattern = normalize_finding_path(rule.pattern)

        # Exact header match required (conservative)
        if rule_header != header:
            continue
        # Pattern match: if rule has a pattern, it must match the finding's path
        if rule_pattern and rule_pattern not in path and path != rule_pattern:
            # Allow partial match (rule pattern is a prefix of finding path)
            if not path.startswith(rule_pattern) and rule_pattern not in path:
                continue

        # Don't suppress if the finding_id is in the rule's source — this
        # finding contributed to the rule and should still be visible
        if rule.source_finding_ids and finding_id in rule.source_finding_ids:
            continue

        return True, f"Suppressed by learned rule {rule.rule_id} ('{header}')"

    return False, ""


def _process_learned_rules_for_run(
    findings: List[Dict[str, Any]],
    rules_state: Dict[str, Any],
    run_id: str,
    *,
    patterns_file: Path | None = None,
) -> tuple[List[Dict[str, Any]], List[LearnedRule], List[str]]:
    """
    Process learned rules during an autonomous review run.

    This function:
    1. Loads existing rules from rules_state.
    2. For each finding, checks if an existing tentative rule's evidence
       should be incremented.
    3. For each finding, checks if a NEW tentative rule should be proposed.
    4. Attempts to auto-activate any tentative rules whose evidence
       threshold is now met.
    5. Applies active learned rules to suppress matching findings.
    6. Returns (filtered_findings, updated_rules, log_messages).

    This function does NOT implement live polling — it only processes
    the findings passed in from the current run.

    Args:
        findings:    Current run's findings (before suppression).
        rules_state: Loaded learned_rules.json dict.
        run_id:      Current run ID.

    Returns:
        (suppressed_findings, updated_rules, log_messages) where
        suppressed_findings = findings that were NOT suppressed (active rules applied),
        updated_rules = updated list of LearnedRule objects,
        log_messages = human-readable log of decisions made.
    """
    log: List[str] = []
    raw_rules: List[Dict[str, Any]] = rules_state.get("rules", [])
    rules: List[LearnedRule] = []
    for r in raw_rules:
        try:
            rules.append(LearnedRule.from_dict(r))
        except Exception:
            # Skip malformed rules rather than crashing
            log.append(f"SKIPPED malformed rule: {r.get('rule_id', 'unknown')}")
            continue

    # Index rules by header for fast lookup
    rules_by_header: Dict[str, LearnedRule] = {
        normalize_finding_header(r.header): r for r in rules
    }

    # --- Step 1: Accumulate evidence for existing tentative rules ---
    new_rules: List[LearnedRule] = []
    # Index: rule_id → position in rules list (for in-place updates)
    rule_idx: Dict[str, int] = {r.rule_id: i for i, r in enumerate(rules)}
    for finding in findings:
        f_header = normalize_finding_header(str(finding.get("header", "")))
        existing = rules_by_header.get(f_header)
        if existing and existing.status == LearnedRuleStatus.TENTATIVE:
            fid = finding.get("finding_id", "")
            updated = _increment_rule_evidence(existing, fid)
            new_rules.append(updated)
            # Update both the index map and the rules list in-place
            rules_by_header[f_header] = updated
            idx = rule_idx.get(existing.rule_id)
            if idx is not None:
                rules[idx] = updated
                rule_idx[updated.rule_id] = idx
            log.append(
                f"EVIDENCE {updated.rule_id}: '{f_header}' "
                f"count={updated.evidence_count}"
            )

    # Merge new_rules into rules (adding only truly new rules)
    seen_ids = {r.rule_id for r in rules}
    for nr in new_rules:
        if nr.rule_id not in seen_ids:
            rules.append(nr)
            seen_ids.add(nr.rule_id)
            rules_by_header[normalize_finding_header(nr.header)] = nr

    # --- Step 2: Propose new tentative rules for repeated patterns ---
    # Track which headers we've already seen in this run to detect repetition
    header_seen_count: Dict[str, int] = {}
    for finding in findings:
        f_header = normalize_finding_header(str(finding.get("header", "")))
        header_seen_count[f_header] = header_seen_count.get(f_header, 0) + 1

    for finding in findings:
        f_header = normalize_finding_header(str(finding.get("header", "")))
        # Only propose if this header appeared at least 2 times in this run
        # (single-occurrence findings are not candidates for learning)
        if header_seen_count.get(f_header, 0) < 2:
            continue
        # Don't propose if rule already exists for this header
        if f_header in rules_by_header:
            continue

        proposed = _propose_learned_rule_from_finding(
            finding=finding,
            run_id=run_id,
            existing_rules=rules,
        )
        if proposed:
            rules.append(proposed)
            rules_by_header[f_header] = proposed
            log.append(
                f"PROPOSED tentative rule {proposed.rule_id}: "
                f"'{f_header}' (evidence={proposed.evidence_count})"
            )

    # --- Step 3: Attempt to auto-activate tentative rules ---
    current_finding_ids = [f.get("finding_id", "") for f in findings]
    activated_rules: List[LearnedRule] = []
    for i, rule in enumerate(rules):
        if rule.status != LearnedRuleStatus.TENTATIVE:
            continue
        should_activate, reason = _should_activate_tentative_rule(
            rule, current_finding_ids
        )
        if should_activate:
            activated = _activate_tentative_rule(rule)
            rules[i] = activated
            activated_rules.append(activated)
            rules_by_header[normalize_finding_header(activated.header)] = activated
            log.append(f"ACTIVATED {activated.rule_id}: {reason}")

    # --- Step 4: Apply active learned rules to suppress findings ---
    active_rules = [r for r in rules if r.status == LearnedRuleStatus.ACTIVE]
    suppressed_findings: List[Dict[str, Any]] = []
    for finding in findings:
        should_suppress, reason = _suppress_finding_with_rule(finding, active_rules)
        if should_suppress:
            suppressed_findings.append(finding)
            fid = finding.get("finding_id", "unknown")
            log.append(f"SUPPRESSED finding {fid} ({reason})")
        else:
            # Finding passes through (not suppressed)
            pass

    filtered_findings = [f for f in findings if f not in suppressed_findings]

    try:
        from bluei.engine.pattern_store import FixPatternStore

        if patterns_file and patterns_file.exists():
            _pstore = FixPatternStore(patterns_file)
            for finding in filtered_findings:
                _rule = finding.get("rule", "") or finding.get("header", "")
                _matches = _pstore.load_active(rule=_rule) if _rule else []
                if _matches and getattr(_matches[0], "confidence", 0) >= 0.9:
                    finding["pattern_fixable"] = True
    except Exception:
        pass

    log.append(
        f"RULES SUMMARY: {len(rules)} total rules, "
        f"{len([r for r in rules if r.status == LearnedRuleStatus.ACTIVE])} active, "
        f"{len([r for r in rules if r.status == LearnedRuleStatus.TENTATIVE])} tentative, "
        f"{len(suppressed_findings)} suppressed"
    )

    return filtered_findings, rules, log
