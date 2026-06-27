"""Tests for bluei.engine.governance — event-sourced governance substrate.

Covers T2.1–T2.9 (PROMPT-03). The substrate must be a no-op with an empty
trail: empty projection, all assets default to ACTIVE.
"""

from bluei.engine.governance import (
    ApprovalRecord,
    PromotionResult,
    find_sprt_reset_boundary,
    format_asset_ref,
    is_governance_active,
    parse_asset_ref,
    project_governance_state,
    resolve_learning_mode,
    resolve_policy,
)


# --- AssetRef helpers -------------------------------------------------------


def test_asset_ref_round_trip():
    s = format_asset_ref("pattern", "py-broad-except-v3")
    assert s == "pattern:py-broad-except-v3"
    cls, oid = parse_asset_ref(s)
    assert (cls, oid) == ("pattern", "py-broad-except-v3")


def test_parse_asset_ref_no_colon():
    # partition with no separator returns (whole_string, "", "").
    cls, oid = parse_asset_ref("noseparator")
    assert cls == "noseparator"
    assert oid == ""


# --- Governance State projection -------------------------------------------


def test_project_governance_state_empty_returns_empty_dict():
    # Critical no-op guarantee: empty trail → empty projection.
    assert project_governance_state([]) == {}


def test_project_governance_state_most_recent_wins():
    records = [
        {"asset_ref": "pattern:a", "decision": "approve", "timestamp": "t1"},
        {"asset_ref": "pattern:a", "decision": "pause", "timestamp": "t2"},
        {"asset_ref": "pattern:b", "decision": "approve", "timestamp": "t3"},
        {"asset_ref": "pattern:a", "decision": "resume", "timestamp": "t4"},
    ]
    state = project_governance_state(records)
    # 'a' progression: active → paused → active (resume wins, most recent).
    assert state["pattern:a"] == "active"
    assert state["pattern:b"] == "active"
    assert len(state) == 2


def test_project_governance_state_ignores_unknown_decisions():
    records = [
        {"asset_ref": "pattern:a", "decision": "approve"},
        {"asset_ref": "pattern:b", "decision": "totally_unknown_decision"},
        {"asset_ref": "pattern:c", "decision": "retire"},
    ]
    state = project_governance_state(records)
    assert state == {"pattern:a": "active", "pattern:c": "retired"}


def test_project_governance_state_skips_missing_fields():
    records = [
        {"asset_ref": "pattern:a", "decision": "approve"},
        {"decision": "approve"},  # missing asset_ref
        {"asset_ref": "pattern:b"},  # missing decision
    ]
    state = project_governance_state(records)
    assert state == {"pattern:a": "active"}


# --- is_governance_active ---------------------------------------------------


def test_is_governance_active_unlisted_ref_defaults_active():
    # No-op guarantee: assets with no governance history are ACTIVE.
    assert is_governance_active("pattern:never_seen", {}) is True


def test_is_governance_active_paused_is_false():
    assert is_governance_active("pattern:x", {"pattern:x": "paused"}) is False


def test_is_governance_active_active_is_true():
    assert is_governance_active("pattern:x", {"pattern:x": "active"}) is True


def test_is_governance_active_retired_is_false():
    assert is_governance_active("pattern:x", {"pattern:x": "retired"}) is False


# --- SPRT reset boundary ----------------------------------------------------


def test_find_sprt_reset_boundary_returns_most_recent_reset():
    records = [
        {"asset_ref": "pattern:a", "decision": "approve", "timestamp": "t1"},
        {"asset_ref": "pattern:a", "decision": "auto_demote", "timestamp": "t2"},
        {"asset_ref": "pattern:a", "decision": "approve", "timestamp": "t3"},
        {"asset_ref": "pattern:a", "decision": "auto_promote", "timestamp": "t4"},
        {"asset_ref": "pattern:a", "decision": "pause", "timestamp": "t5"},
    ]
    # Reverse scan: t5 pause (no), t4 auto_promote (hit) → returns t4.
    assert find_sprt_reset_boundary(records, "pattern:a") == "t4"


def test_find_sprt_reset_boundary_resume_counts():
    records = [
        {"asset_ref": "pattern:a", "decision": "resume", "timestamp": "t1"},
        {"asset_ref": "pattern:a", "decision": "approve", "timestamp": "t2"},
    ]
    # t2 approve (no), t1 resume (hit) → returns t1.
    assert find_sprt_reset_boundary(records, "pattern:a") == "t1"


def test_find_sprt_reset_boundary_none_when_no_reset():
    records = [
        {"asset_ref": "pattern:a", "decision": "approve", "timestamp": "t1"},
        {"asset_ref": "pattern:a", "decision": "pause", "timestamp": "t2"},
    ]
    assert find_sprt_reset_boundary(records, "pattern:a") is None


def test_find_sprt_reset_boundary_scoped_to_asset():
    records = [
        {"asset_ref": "pattern:a", "decision": "auto_demote", "timestamp": "t1"},
        {"asset_ref": "pattern:b", "decision": "auto_promote", "timestamp": "t2"},
    ]
    # Only pattern:a matches; pattern:b's reset is ignored.
    assert find_sprt_reset_boundary(records, "pattern:a") == "t1"


# --- Policy resolution ------------------------------------------------------


def test_resolve_learning_mode_default_active():
    assert resolve_learning_mode({}) == "active"
    assert resolve_learning_mode({"learning": {}}) == "active"


def test_resolve_policy_active_mode_returns_per_class_value():
    config = {
        "learning": {
            "mode": "active",
            "asset_classes": {
                "pattern": {"write_producing": "gate_closed"},
            },
        }
    }
    assert resolve_policy(config, "pattern", "write_producing") == "gate_closed"


def test_resolve_policy_active_mode_defaults_gate_open_audit():
    config = {"learning": {"mode": "active"}}
    # No asset_classes / no transition configured → default gate_open_audit.
    assert resolve_policy(config, "pattern", "write_producing") == "gate_open_audit"


def test_resolve_policy_paused_mode_returns_paused():
    config = {
        "learning": {
            "mode": "paused",
            "asset_classes": {"pattern": {"write_producing": "gate_open_audit"}},
        }
    }
    # Paused ceiling overrides everything.
    assert resolve_policy(config, "pattern", "write_producing") == "paused"


def test_resolve_policy_audit_only_forces_gate_open_audit_to_closed():
    config = {
        "learning": {
            "mode": "audit_only",
            "asset_classes": {
                "pattern": {"in_place_mutation": "gate_open_audit"},
            },
        }
    }
    # audit_only ceiling forces gate_open_audit → gate_closed.
    assert resolve_policy(config, "pattern", "in_place_mutation") == "gate_closed"


def test_resolve_policy_audit_only_keeps_gate_closed():
    config = {
        "learning": {
            "mode": "audit_only",
            "asset_classes": {"pattern": {"write_producing": "gate_closed"}},
        }
    }
    # gate_closed is below the ceiling — left as-is.
    assert resolve_policy(config, "pattern", "write_producing") == "gate_closed"


# --- ApprovalRecord + PromotionResult --------------------------------------


def test_approval_record_to_dict_round_trip():
    rec = ApprovalRecord(
        asset_ref="pattern:a",
        decision="approve",
        native_state_before="pending",
        native_state_after="active",
        reason="operator approved",
        evidence_snapshot={"k": "v"},
        actor="operator",
        timestamp="2026-01-01T00:00:00Z",
    )
    d = rec.to_dict()
    assert d["asset_ref"] == "pattern:a"
    assert d["decision"] == "approve"
    assert d["evidence_snapshot"] == {"k": "v"}
    # The dict should be projectable back into Governance State.
    assert project_governance_state([d]) == {"pattern:a": "active"}


def test_approval_record_default_collections_independent():
    # Each instance gets its own evidence_snapshot (no shared mutable default).
    a = ApprovalRecord(
        asset_ref="x",
        decision="approve",
        native_state_before="",
        native_state_after="",
        reason="",
    )
    b = ApprovalRecord(
        asset_ref="y",
        decision="pause",
        native_state_before="",
        native_state_after="",
        reason="",
    )
    a.evidence_snapshot["k"] = "v"
    assert b.evidence_snapshot == {}


def test_promotion_result_enum_values():
    assert PromotionResult.NOT_ELIGIBLE.value == "not_eligible"
    assert PromotionResult.PENDING_APPROVAL.value == "pending_approval"
    assert PromotionResult.PROMOTED.value == "promoted"
    # str Enum — usable as a string.
    assert PromotionResult.PROMOTED == "promoted"
