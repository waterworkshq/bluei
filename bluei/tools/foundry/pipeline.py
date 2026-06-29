"""Foundry pipeline — orchestrate proposal -> validation -> policy-routed write.

For each Pattern: propose_recipe -> resolve_policy(config, "recipe",
"write_producing"):
  - gate_open_audit (default): write YAML to staged_dir + append auto_promote.
  - gate_closed: append pending_approval with proposed_recipe_yaml cached in
    evidence_snapshot (ADR-0020 carve-out from ADR-0007 — Foundry is a trusted
    author; does NOT call has_bundle_reference).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.governance import (
    ApprovalRecord,
    format_asset_ref,
    resolve_policy,
)
from bluei.engine.jsonl import append_jsonl
from bluei.engine.models import now_iso
from bluei.engine.pattern_store import FixPattern
from bluei.tools.foundry.proposer import propose_recipe


@dataclass
class FoundrySummary:
    """Outcome counters for a single ``run_foundry`` invocation."""

    proposed: int = 0
    rejected: int = 0
    rejected_topics: List[str] = field(default_factory=list)
    written: int = 0
    queued_pending: int = 0
    auto_promoted: int = 0


def run_foundry(
    patterns: List[FixPattern],
    bundles: List[GoldenBundle],
    *,
    llm_callback: Callable[[str], str],
    staged_dir: Path,
    config: Optional[Dict[str, Any]] = None,
    approval_records_path: Optional[Path] = None,
    ruff_executable: str = "ruff",
    eslint_executable: str = "eslint",
) -> FoundrySummary:
    """Orchestrate proposal -> validation -> policy-routed write per Pattern."""
    config = config or {}
    bundles_by_rule = {b.rule: b for b in bundles}
    summary = FoundrySummary()

    for pattern in patterns:
        proposal = propose_recipe(
            pattern,
            bundles_by_rule.get(pattern.rule),
            llm_callback,
            ruff_executable=ruff_executable,
            eslint_executable=eslint_executable,
        )

        if proposal is None:
            summary.rejected += 1
            summary.rejected_topics.append(pattern.rule)
            continue

        if not proposal.detector_passed:
            summary.rejected += 1
            summary.rejected_topics.append(pattern.rule)
            continue

        summary.proposed += 1
        recipe_id = proposal.parsed.id
        asset_ref = format_asset_ref("recipe", recipe_id)
        policy = resolve_policy(config, "recipe", "write_producing")

        if policy == "gate_closed":
            if approval_records_path is not None:
                record = ApprovalRecord(
                    asset_ref=asset_ref,
                    decision="pending_approval",
                    native_state_before="seeded-pattern",
                    native_state_after="pending-recipe",
                    reason="Foundry proposal; gate-closed policy requires approval",
                    evidence_snapshot={
                        "recipe_id": recipe_id,
                        "proposed_recipe_yaml": proposal.recipe_yaml,
                        "source_pattern_id": proposal.source_pattern_id,
                        "source_bundle_id": proposal.source_bundle_id,
                        "rule": pattern.rule,
                    },
                    actor="system:recipe-foundry",
                    timestamp=now_iso(),
                )
                append_jsonl(approval_records_path, record.to_dict())
            summary.queued_pending += 1
            continue

        staged_dir.mkdir(parents=True, exist_ok=True)
        out_path = staged_dir / f"{recipe_id}.yaml"
        out_path.write_text(proposal.recipe_yaml, encoding="utf-8")
        summary.written += 1

        if approval_records_path is not None:
            record = ApprovalRecord(
                asset_ref=asset_ref,
                decision="auto_promote",
                native_state_before="seeded-pattern",
                native_state_after="staged-recipe",
                reason="Foundry auto-promotion via gate-open policy",
                evidence_snapshot={
                    "recipe_id": recipe_id,
                    "source_pattern_id": proposal.source_pattern_id,
                    "source_bundle_id": proposal.source_bundle_id,
                    "rule": pattern.rule,
                },
                actor="system:recipe-foundry",
                timestamp=now_iso(),
            )
            append_jsonl(approval_records_path, record.to_dict())
        summary.auto_promoted += 1

    return summary
