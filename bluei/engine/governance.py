"""bluei.engine.governance — event-sourced governance substrate (ADR-0006/0007/0008).

Data layer for the Operator Control Plane. Holds:

* ``AssetRef`` helpers — canonical ``"<asset_class>:<asset_id>"`` string form.
* ``ApprovalRecord`` — append-only decision event written to
  ``state/approval_records.jsonl``.
* ``PromotionResult`` — outcome enum consumed by promotion gates.
* ``project_governance_state`` — read-time projection of the ApprovalRecord
  trail (most-recent-wins per asset_ref).
* ``is_governance_active`` — consultation predicate (default ACTIVE).
* ``find_sprt_reset_boundary`` — most recent auto_demote/auto_promote/resume
  for an asset (used by SPRT LLR recomputation in Phase 7).
* ``resolve_policy`` / ``resolve_learning_mode`` — per-class transition policy
  with the learning-mode ceiling.

The substrate is a no-op until the first ApprovalRecord is written: an empty
trail yields an empty projection, and ``is_governance_active`` returns ``True``
for every asset_ref (default ``"active"``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PromotionResult(str, Enum):
    """Outcome of a promotion attempt (ADR-0007)."""

    NOT_ELIGIBLE = "not_eligible"  # check_promotion_eligibility failed
    PENDING_APPROVAL = "pending_approval"  # gate-closed; ApprovalRecord queued
    PROMOTED = "promoted"  # gate-open or approved; YAML written


@dataclass
class ApprovalRecord:
    """A single append-only governance decision event (ADR-0008)."""

    asset_ref: str
    decision: str
    native_state_before: str
    native_state_after: str
    reason: str
    evidence_snapshot: Dict[str, Any] = field(default_factory=dict)
    actor: str = ""
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict for JSONL persistence."""
        return {
            "asset_ref": self.asset_ref,
            "decision": self.decision,
            "native_state_before": self.native_state_before,
            "native_state_after": self.native_state_after,
            "reason": self.reason,
            "evidence_snapshot": dict(self.evidence_snapshot),
            "actor": self.actor,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# AssetRef helpers
# ---------------------------------------------------------------------------


def format_asset_ref(asset_class: str, asset_id: str) -> str:
    """Render an AssetRef as ``"<asset_class>:<asset_id>"``."""
    return f"{asset_class}:{asset_id}"


def parse_asset_ref(s: str) -> tuple[str, str]:
    """Split an AssetRef string back into ``(asset_class, asset_id)``."""
    cls, _, oid = s.partition(":")
    return cls, oid


# ---------------------------------------------------------------------------
# Governance State projection
# ---------------------------------------------------------------------------

# Decision-to-Governance-State mapping (DESIGN.md §A.3).
_DECISION_TO_STATE: Dict[str, str] = {
    "pending_approval": "pending",
    "approve": "active",
    "reject": "retired",
    "pause": "paused",
    "resume": "active",
    "retire": "retired",
    "auto_demote": "paused",
    "auto_promote": "active",
}


def project_governance_state(records: List[Dict[str, Any]]) -> Dict[str, str]:
    """Project the ApprovalRecord trail into a ``{asset_ref: state}`` dict.

    Most-recent-wins per asset_ref. Returns ``{}`` for an empty trail. Unlisted
    asset_refs default to ``"active"`` — that default is applied by
    :func:`is_governance_active`, not stored in this dict.
    """
    state: Dict[str, str] = {}
    for r in records:
        asset_ref = r.get("asset_ref")
        decision = r.get("decision")
        if asset_ref is None or decision is None:
            continue
        mapped = _DECISION_TO_STATE.get(decision)
        if mapped is None:
            continue
        state[asset_ref] = mapped
    return state


def is_governance_active(asset_ref: str, governance_state: Dict[str, str]) -> bool:
    """Return True if the asset is ACTIVE under the projected Governance State.

    Defaults to ACTIVE for unlisted refs — the entire substrate is a no-op
    until the first ApprovalRecord is written.
    """
    return governance_state.get(asset_ref, "active") == "active"


# ---------------------------------------------------------------------------
# SPRT reset boundary
# ---------------------------------------------------------------------------


def find_sprt_reset_boundary(
    records: List[Dict[str, Any]], asset_ref: str
) -> Optional[str]:
    """Return the timestamp of the most recent SPRT reset for ``asset_ref``.

    Scans the record list in REVERSE order and returns the timestamp of the
    first record whose ``decision`` is ``auto_demote``, ``auto_promote``, or
    ``resume``. Returns ``None`` when no reset exists (SPRT then uses all
    records for the asset).
    """
    for r in reversed(records):
        if r.get("asset_ref") == asset_ref and r.get("decision") in (
            "auto_demote",
            "auto_promote",
            "resume",
        ):
            return r.get("timestamp")
    return None


# ---------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------


def resolve_learning_mode(config: Dict[str, Any]) -> str:
    """Return the global learning mode (``active`` | ``audit_only`` | ``paused``)."""
    return config.get("learning", {}).get("mode", "active")


def resolve_policy(config: Dict[str, Any], asset_class: str, transition: str) -> str:
    """Resolve the effective policy for a per-class transition (DESIGN.md §E.2).

    Returns ``"gate_closed"``, ``"gate_open_audit"``, or ``"paused"``. The
    global ``learning.mode`` acts as a ceiling:

    * ``paused`` → every transition is paused.
    * ``audit_only`` → gate-open transitions are forced to gate-closed.
    * otherwise → the per-class transition value (default ``gate_open_audit``).
    """
    mode = resolve_learning_mode(config)
    if mode == "paused":
        return "paused"
    class_cfg = config.get("learning", {}).get("asset_classes", {}).get(asset_class, {})
    raw = class_cfg.get(transition, "gate_open_audit")
    if mode == "audit_only" and raw == "gate_open_audit":
        return "gate_closed"
    return raw
