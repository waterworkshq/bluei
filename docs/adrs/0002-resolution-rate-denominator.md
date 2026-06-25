# ADR-0002: Resolution rates use findings_attempted as the canonical denominator

**Status:** accepted

All Flywheel Ledger resolution/fallback rates (deterministic resolution rate, LLM fallback rate) use **`findings_attempted`** — Findings the cascade actually ran on — as their denominator, never `findings_detected` or `findings_unsuppressed`. Every rate renders as `numerator/denominator (%)` so the base can never float free of the number. Detection and suppression counts are shown as separate context rows, never silently folded into the denominator.

## Considered Options

- **(A) `findings_attempted`.** ✅ chosen — the honest activity base; suppressed/refactor-routed/skipped Findings didn't test the flywheel.
- (B) `findings_detected` (÷100). Rejected: detection volume swings week to week and would make a healthy flywheel *appear* to regress.
- (C) `findings_unsuppressed`. Rejected: still conflates routing decisions with flywheel performance.

## Consequences

- "Cost per verified fix" reuses the existing `fixes_verified` from `finalize.py` (not a new definition of "verified").
- Operators building intuition around a published metric need a stable base; changing this denominator later silently re-bases every historical comparison, so it is published once and frozen.
