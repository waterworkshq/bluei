"""Foundry pipeline — orchestration proposal -> validation -> policy-routed write.

For each Pattern: propose_recipe -> resolve_policy(config, "recipe",
"write_producing"):
  - gate_closed (explicit config): append pending_approval with
    proposed_recipe_yaml cached in evidence_snapshot (ADR-0020 carve-out
    from ADR-0007 — Foundry is a trusted author; does NOT call
    has_bundle_reference).
  - gate_open_audit (the default for unconfigured ``write_producing`` at
    this BUILD-TIME gate — the human commit that runs the pipeline IS the
    approval, per the ADR-0007 refinement + ADR-0020; the RUNTIME promoter
    stays gate-closed by default): write YAML to staged_dir + append
    auto_promote.
  - paused (learning.mode=paused): hard hold (ADR-0013) — no YAML, no
    pending record; counted in ``summary.held_paused``.
"""

from __future__ import annotations

import logging
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
from bluei.engine.recipe_schema import is_safe_recipe_id
from bluei.tools.foundry.proposer import propose_recipe

_logger = logging.getLogger(__name__)


@dataclass
class FoundrySummary:
    """Outcome counters for a single ``run_foundry`` invocation."""

    proposed: int = 0
    rejected: int = 0
    rejected_topics: List[str] = field(default_factory=list)
    written: int = 0
    queued_pending: int = 0
    auto_promoted: int = 0
    held_paused: int = 0


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
    retained: Optional[dict] = None,
    repo_path: Optional[Path] = None,
    checks: Optional[list] = None,
) -> FoundrySummary:
    """Orchestrate proposal -> validation -> policy-routed write per Pattern."""
    config = config or {}
    bundles_by_rule = {b.rule: b for b in bundles}
    summary = FoundrySummary()

    for pattern in patterns:
        bundle = bundles_by_rule.get(pattern.rule)
        extra = {}
        if retained is not None:
            matching = [b for b in bundles if b.asset_ref == retained.get("shared_asset_ref")
                        and b.id == f"gb-{retained['evidence_id']}"
                        and b.before == retained['before'] and b.after == retained['after']]
            if (len(matching) != 1 or pattern.rule != retained['rule']
                    or f"pattern:{pattern.pattern_id}" != retained['local_asset_ref']):
                summary.rejected += 1
                continue
            bundle = matching[0]
            extra = {"retained": retained, "repo_path": repo_path, "checks": checks}
        proposal = propose_recipe(
            pattern, bundle, llm_callback, ruff_executable=ruff_executable,
            eslint_executable=eslint_executable, **extra,
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
        if not is_safe_recipe_id(recipe_id):
            # D3-03 defense-in-depth: parse_recipe (the schema gate) already
            # rejects unsafe ids, so this guards the write path against any
            # proposal that bypassed the gate — the id is used verbatim as a
            # filename below, so a path-like id must never reach the write.
            _logger.warning(
                "foundry: rejecting recipe with unsafe id %r (rule=%s)",
                recipe_id,
                pattern.rule,
            )
            summary.rejected += 1
            summary.rejected_topics.append(pattern.rule)
            continue
        asset_ref = format_asset_ref("recipe", recipe_id)
        # Build-time gate (ADR-0007 refinement + ADR-0020): the Foundry is a
        # curated pipeline — the human commit that runs it IS the approval,
        # so unconfigured write_producing defaults gate-open-with-audit
        # here (the runtime promoter stays gate-closed by default).
        policy = resolve_policy(
            config, "recipe", "write_producing", default="gate_open_audit"
        )

        if policy == "paused":
            # ADR-0013 kill switch: paused is a HARD HOLD — strictly stronger
            # than gate_closed (no YAML write, no pending record). Counted in
            # held_paused + logged so operators can see the switch engaged.
            _logger.warning(
                "foundry: promotion held for %s — learning.mode=paused",
                asset_ref,
            )
            summary.held_paused += 1
            continue

        if retained is not None:
            import hashlib

            from bluei.engine.retained_recipe import (
                append_decision, bound_artifact, digest, latest, stage_exact, stage_path,
                trail_lock, trail_path,
            )

            # Runtime evidence uses the existing write-producing default, not
            # the build-time seed exception.
            policy = resolve_policy(config, "recipe", "write_producing")
            if approval_records_path is None:
                summary.rejected += 1
                continue
            state_dir = approval_records_path.parent
            if approval_records_path != trail_path(state_dir):
                summary.rejected += 1
                continue
            proof = {
                "contract": "retained-ruff-b904-v1", "repo_id": retained["repo_id"],
                "asset_ref": asset_ref, "evidence_sha256": digest(retained),
                "source_pattern_id": proposal.source_pattern_id,
                "source_bundle_id": bundle.id, "shared_asset_ref": bundle.asset_ref,
                "checks_sha256": digest(checks), "proposed_recipe_yaml": proposal.recipe_yaml,
                "recipe_sha256": hashlib.sha256(proposal.recipe_yaml.encode()).hexdigest(),
            }
            proof["proposal_id"] = digest(proof)
            with trail_lock(state_dir) as trail:
                # Recheck after authoring and validation: neither can bless a
                # patch or evidence record that changed while they were running.
                _, recipe, artifact = bound_artifact(
                    {"asset_ref": asset_ref, "evidence_snapshot": proof}, retained["repo_id"], state_dir,
                )
                previous = latest(trail, asset_ref)
                if previous:
                    if previous.get("decision") == "pending_approval" and previous.get("evidence_snapshot") == proof:
                        summary.queued_pending += 1
                    else:
                        summary.rejected += 1
                    continue
                if policy == "gate_closed":
                    append_decision(trail, asset_ref, "pending_approval", "accepted-pattern",
                                    "pending-recipe", proof, "system:recipe-foundry")
                    summary.queued_pending += 1
                else:
                    stage_exact(stage_path(state_dir, recipe), artifact)
                    append_decision(trail, asset_ref, "auto_promote", "accepted-pattern",
                                    "staged-retained-recipe", proof, "system:recipe-foundry")
                    summary.written += 1
                    summary.auto_promoted += 1
            continue

        if policy == "gate_closed":
            # A curated seed has no registered retained evidence. The public
            # inbox cannot act on it, so do not queue an unbound proposal.
            _logger.warning("foundry: %s requires registered retained evidence", asset_ref)
            summary.rejected += 1
            summary.rejected_topics.append(pattern.rule)
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
