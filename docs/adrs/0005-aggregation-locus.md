# ADR-0005: Per-cycle Ledger summary is computed at write-time into status.json

**Status:** accepted

The per-cycle report card and dashboard card read a **pre-aggregated** `flywheel_ledger` block that `finalize` writes into `status.json` — the file `extract_report_data` and `build_dashboard_data` already load. finalize builds the block from its live counters and cost tracker *before* they are torn down, so no cross-file correlation key is needed. The raw `cascade_resolutions.jsonl` sink (ADR-0001) is retained as the durable append-only record for cumulative/cross-cycle views, but the read-time aggregation *code* is deferred to the features that need it (alpha.2 / the deferred `bluei savings` CLI).

## Considered Options

- **(A) Hybrid: write-time summary in `status.json` + retained raw sink (read-time deferred).** ✅ chosen.
- (B) Read-time aggregation over both JSONLs joined by `run_id`. Rejected: `cost_log` entries carry no `run_id` today (gap), requires new aggregation code in two render paths, and re-computes data the run already computed once.
- (C) Write-time only, no raw sink. Rejected: no durable record for cumulative views or the future CLI; the per-cycle summary alone is insufficient for the flywheel's cross-cycle story.

## Consequences

- `status.json` gains an additive `flywheel_ledger` key per cycle; old readers ignore unknown keys (no schema break).
- **Per-stage detail crosses the `apply_cascade_fix`→`bool` boundary via an in-memory accumulator** (`CascadeContext.ledger_records: List[Dict]`), not via pr_cycle guessing the stage and not via sink re-read. The cascade's emission point (ADR-0001) appends each resolution record to *both* the durable `cascade_resolutions.jsonl` and the accumulator; standalone-replay HITs append to the same accumulator. `finalize` reads `ctx.ledger_records` (populated during the cycle) for the full per-stage + coarse + replay block. No separate `_LedgerCounters` class.
- **`run_id` is deferred out of alpha.1.** Since the per-cycle block reads the in-memory accumulator (no cross-file join), no `run_id` is needed this release; sink rows omit it. `run_id` is added when cumulative/cross-cycle features (alpha.2 / `bluei savings`) require sink re-read + join.
- Legacy `claude_invocations`/`deterministic_invocations` remain in `status.json` for backward compatibility but are **not rendered** in ledger surfaces (they are incomplete — they miss standalone-replay wins and conflate attempts with resolutions); ledger metrics are authoritative to avoid two conflicting "deterministic" numbers.
