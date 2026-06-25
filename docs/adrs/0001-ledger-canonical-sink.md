# ADR-0001: Flywheel Ledger uses a dedicated cascade_resolutions.jsonl sink

**Status:** accepted

The Deterministic Flywheel's value is buried today: `CascadeTelemetry` is written only as text into the run log (`cascade.py:_write_telemetry`) and never aggregated, and `CascadeResult` is discarded at the `apply_cascade_fix` → `bool` boundary (`lifecycle.py:770`). We add a structured per-Finding resolution sink, `cascade_resolutions.jsonl`, kept **separate** from `cost_log.jsonl`, emitted from the resolution paths (the cascade on resolve/exhaust, and pr_cycle directly on standalone-replay HIT — see Consequences) rather than by changing `apply_cascade_fix`'s return type.

## Considered Options

- **(A) Dedicated `cascade_resolutions.jsonl`, emitted inside the cascade.** ✅ chosen.
- (B) Reuse `cost_log.jsonl` by adding a third entry type alongside invocations and `pattern_replay_savings`. Rejected: cost and resolution are different concerns (a Linter-stage win has zero cost-record footprint), and `CostTracker.summary()` would be muddied.
- (C) Change `apply_cascade_fix` to return a structured result so the caller records. Rejected: ripples through every caller and ~170 tests; violates the release's "no fix behavior changes" constraint.

## Consequences

- One row per Finding (resolved or exhausted), uniform shape regardless of which path produced it. Emission happens at two points — inside the cascade (cascade-resolved/exhausted) and in pr_cycle (standalone-replay HIT, which short-circuits the cascade); the `via` field distinguishes them.
- `CascadeContext` gains `ledger_path` + `run_id` (the cascade cannot see the state dir today — `log_file` lives in `logs/` per `DEFAULT_LOG`, while `cost_log.jsonl` and the new sink are siblings at `state_file.parent`, i.e. wherever `state_file` resolves at runtime).
- Cascade log-file telemetry remains as-is for debug; the JSONL sink is the aggregation source.
