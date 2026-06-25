# ADR-0005: Per-cycle Ledger summary is computed at write-time into status.json

**Status:** accepted

The per-cycle report card and dashboard card read a **pre-aggregated** `flywheel_ledger` block that `finalize` writes into `status.json` — the file `extract_report_data` and `build_dashboard_data` already load. finalize builds the block from its live counters and cost tracker *before* they are torn down, so no cross-file correlation key is needed. The raw `cascade_resolutions.jsonl` sink (ADR-0001) is retained as the durable append-only record for cumulative/cross-cycle views, but the read-time aggregation *code* is deferred to the features that need it (alpha.2 / the deferred `bluei savings` CLI).

## Considered Options

- **(A) Hybrid: write-time summary in `status.json` + retained raw sink (read-time deferred).** ✅ chosen.
- (B) Read-time aggregation over both JSONLs joined by `run_id`. Rejected: `cost_log` entries carry no `run_id` today (gap), requires new aggregation code in two render paths, and re-computes data the run already computed once.
- (C) Write-time only, no raw sink. Rejected: no durable record for cumulative views or the future CLI; the per-cycle summary alone is insufficient for the flywheel's cross-cycle story.

## Consequences

- `status.json` gains an additive `flywheel_ledger` key per cycle; old readers ignore unknown keys (no schema break).
- Legacy `claude_invocations`/`deterministic_invocations` remain in `status.json` for backward compatibility but are **not rendered** in ledger surfaces (they are incomplete — they miss standalone-replay wins and conflate attempts with resolutions); ledger metrics are authoritative to avoid two conflicting "deterministic" numbers.
- When cumulative aggregation is needed, `cascade_resolutions.jsonl` + `cost_log.jsonl` join by timestamp window or a then-added `run_id`.
