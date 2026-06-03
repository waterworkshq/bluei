"""escalation.py — Backward-compat re-exports from engine.escalation.

All escalation logic now lives in bluei.engine.escalation.
This module re-exports the two app-layer functions for backward compat."""

from bluei.engine.escalation import (  # noqa: F401
    DEFAULT_ESCALATION_FILE,
    check_escalation_status,
    write_escalation,
)
