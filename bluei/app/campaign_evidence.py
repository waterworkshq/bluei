"""App-layer consumer for campaign emergent-rule evidence.

This module exists to keep :mod:`bluei.campaigns.executor` free of
``bluei.app.*`` imports (campaigns→app is a layering violation). The
:class:`~bluei.campaigns.executor.CampaignExecutor` accepts an injected
``emergent_evidence_consumer`` callable; this module provides the canonical
implementation, wired at :mod:`bin.cmd_campaign`. All imports here are
app→app + app→engine (both legal).

The consumer advances the named emergent rule via shadow validation AND
proposes new rules from successful fix patterns. It mutates only the emergent
store JSON — never the governance ApprovalRecord trail (AC-P2-4).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from bluei.app.emergent_rules import (
    EmergentRuleStore,
    measure_false_positives,
    propose_rules_from_fix_patterns,
    scan_shadow_rules,
)
from bluei.engine.pattern_store import FixPatternStore

_logger = logging.getLogger(__name__)


def consume_emergent_evidence(
    *,
    target_ref: str,
    worktree: Path,
    run_id: str,
    pattern_store_path: Optional[Path],
    emergent_store_path: Path,
) -> Dict[str, Any]:
    """Advance the named emergent rule + propose new rules from fix patterns.

    (1) ``scan_shadow_rules`` + ``record_shadow_run`` advance the target
    rule's shadow lifecycle (CANDIDATE→TENTATIVE→ACTIVE). (2) successful
    fix patterns (success_count >= 5, confidence >= 0.5) feed
    ``propose_rules_from_fix_patterns`` which appends PROPOSED rules to the
    store. Both mutate the emergent store JSON only — zero ApprovalRecord
    writes (AC-P2-4).

    Args:
        target_ref: Emergent rule_id (``er-...``) to advance.
        worktree: Root directory of the repository to shadow-scan.
        run_id: Identifier stamped on proposed rules.
        pattern_store_path: Optional FixPatternStore JSONL file. When present
            and existing, successful patterns drive new-rule proposals.
        emergent_store_path: EmergentRuleStore JSON file to advance + append.

    Returns:
        Dict with ``assets_proven`` (rule_ids that reached ACTIVE this call)
        and ``proposed_rules`` (rule_ids appended this call).
    """
    assets_proven: List[str] = []
    proposed_rule_ids: List[str] = []

    store = EmergentRuleStore(emergent_store_path)
    try:
        rules = store.load()
    except Exception:
        _logger.debug("campaign emergent store load failed")
        return {"assets_proven": assets_proven, "proposed_rules": proposed_rule_ids}

    target = next((r for r in rules if r.rule_id == target_ref), None)
    if target is not None:
        try:
            matches = scan_shadow_rules([target], worktree)
            fps = measure_false_positives(target)
            result = store.record_shadow_run(
                target_ref,
                matches=len(matches),
                false_positives=fps,
            )
            if result.get("status") == "active" and target_ref not in assets_proven:
                assets_proven.append(target_ref)
        except KeyError:
            _logger.debug("campaign emergent target %s missing", target_ref)
        except Exception:
            _logger.debug("campaign emergent record_shadow_run failed")

    # Discovery: propose new rules from successful fix patterns.
    if pattern_store_path is not None and pattern_store_path.exists():
        try:
            pattern_store = FixPatternStore(pattern_store_path)
            patterns = pattern_store.load_active()
            proposals = propose_rules_from_fix_patterns(
                patterns, run_id=run_id or "campaign"
            )
        except Exception:
            _logger.debug("campaign fix-pattern proposal failed")
            proposals = []
        if proposals:
            existing_ids = {r.rule_id for r in store.load()}
            appended = [rule for rule in proposals if rule.rule_id not in existing_ids]
            if appended:
                store.save(store.load() + appended)
                for rule in appended:
                    if rule.rule_id not in proposed_rule_ids:
                        proposed_rule_ids.append(rule.rule_id)

    return {"assets_proven": assets_proven, "proposed_rules": proposed_rule_ids}
