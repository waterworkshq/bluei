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

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)


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


# Per-transition defaults (ADR-0007): write-producing transitions are
# GATE-CLOSED by default — "nothing writes without ``bluei learn approve``".
# Every other transition (in-place mutations such as confidence-threshold
# crossings and Emergent Rule TENTATIVE→ACTIVE) defaults gate-open-with-audit.
_TRANSITION_DEFAULTS: Dict[str, str] = {"write_producing": "gate_closed"}
_DEFAULT_TRANSITION_POLICY = "gate_open_audit"

# Raw per-class config values are only honored when exactly one of these two
# gate policies. Anything else (e.g. a typo like "gate_clsoed") would
# otherwise be returned as-is and treated as gate-OPEN by the gate sites —
# validation here makes the resolution fail safe instead of fail open.
_KNOWN_GATE_POLICIES = frozenset({"gate_closed", "gate_open_audit"})


def resolve_policy(
    config: Dict[str, Any],
    asset_class: str,
    transition: str,
    *,
    default: Optional[str] = None,
) -> str:
    """Resolve the effective policy for a per-class transition (DESIGN.md §E.2).

    Returns ``"gate_closed"``, ``"gate_open_audit"``, or ``"paused"``. The
    global ``learning.mode`` acts as a ceiling:

    * ``paused`` → every transition is paused.
    * ``audit_only`` → gate-open transitions are forced to gate-closed.
    * otherwise → the per-class transition value. Unconfigured transitions
      fall back to ``default`` when the caller provided one, else to the
      per-transition table (ADR-0007): ``write_producing`` →
      ``gate_closed`` (a runtime write-producing promotion never defaults
      open), every other transition → ``gate_open_audit``.

    The ``default`` keyword implements the ADR-0007 refinement (runtime vs
    build-time): the RUNTIME promoter passes ``default="gate_closed"`` — no
    write without an explicit gate — while build-time curated pipelines
    (the Foundry) pass ``default="gate_open_audit"``: the human commit that
    runs the pipeline IS the approval (ADR-0020). An explicit per-class
    config value always wins over any default.

    Raw per-class values that are not exactly ``"gate_closed"`` or
    ``"gate_open_audit"`` are rejected with a warning and fall back to the
    effective default (fail closed at runtime) — an unrecognized string
    must not silently behave as gate-open.
    """
    mode = resolve_learning_mode(config)
    if mode == "paused":
        return "paused"

    table_default = _TRANSITION_DEFAULTS.get(transition, _DEFAULT_TRANSITION_POLICY)
    effective_default = table_default
    if default is not None:
        if default in _KNOWN_GATE_POLICIES:
            effective_default = default
        else:
            _logger.warning(
                "resolve_policy: caller passed unknown default %r for %s/%s; "
                "using per-transition default %r",
                default,
                asset_class,
                transition,
                table_default,
            )

    class_cfg = config.get("learning", {}).get("asset_classes", {}).get(asset_class, {})
    raw = class_cfg.get(transition, effective_default)
    # isinstance first: a YAML list/mapping is unhashable and would raise
    # TypeError on the membership test instead of taking the fallback.
    if not isinstance(raw, str) or raw not in _KNOWN_GATE_POLICIES:
        _logger.warning(
            "resolve_policy: unknown policy %r for %s/%s; falling back to %r",
            raw,
            asset_class,
            transition,
            effective_default,
        )
        raw = effective_default
    if mode == "audit_only" and raw == "gate_open_audit":
        return "gate_closed"
    return raw
