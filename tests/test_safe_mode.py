"""Phase 5 (safe-mode) tests — learning.mode tri-state and RunContext wiring.

The policy ceiling logic for ``resolve_policy`` is exercised comprehensively in
``tests/test_governance.py``. This file covers the explicit ``learning.mode``
return values for ``resolve_learning_mode`` and the new ``RunContext`` field,
so that a regression in either is caught independently.
"""

from __future__ import annotations

from bluei.engine.commands.context import RunContext
from bluei.engine.governance import resolve_learning_mode, resolve_policy


# --- resolve_learning_mode return values ------------------------------------


def test_resolve_learning_mode_paused():
    assert resolve_learning_mode({"learning": {"mode": "paused"}}) == "paused"


def test_resolve_learning_mode_audit_only():
    assert resolve_learning_mode({"learning": {"mode": "audit_only"}}) == "audit_only"


# --- RunContext wiring ------------------------------------------------------


def test_runcontext_learning_mode_defaults_active():
    """RunContext.learning_mode defaults to ``active`` (ADR-0013)."""
    import argparse

    ctx = RunContext(args=argparse.Namespace())
    assert ctx.learning_mode == "active"


# --- resolve_policy ceiling regression guards -------------------------------
# These mirror the per-mode ceiling contract Phases 6-7 will rely on.


def test_policy_paused_overrides_gate_open_audit():
    config = {
        "learning": {
            "mode": "paused",
            "asset_classes": {"pattern": {"write_producing": "gate_open_audit"}},
        }
    }
    assert resolve_policy(config, "pattern", "write_producing") == "paused"


def test_policy_audit_only_forces_gate_open_audit_to_closed():
    config = {
        "learning": {
            "mode": "audit_only",
            "asset_classes": {"pattern": {"write_producing": "gate_open_audit"}},
        }
    }
    assert resolve_policy(config, "pattern", "write_producing") == "gate_closed"


def test_policy_active_respects_per_class_setting():
    config = {
        "learning": {
            "mode": "active",
            "asset_classes": {"pattern": {"write_producing": "gate_open_audit"}},
        }
    }
    assert resolve_policy(config, "pattern", "write_producing") == "gate_open_audit"
