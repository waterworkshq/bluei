# ADR-0010: Cumulative ledger sinks carry a run_id correlation key

**Status:** accepted

`cascade_resolutions.jsonl`, `cost_log.jsonl`, and `dry_replay.jsonl` rows carry a `run_id` (UUID4, generated per CLI invocation in `engine/cli.py` alongside `CostTracker` creation) so that cumulative features can join records across cycles by the originating run.

## Why

Dry Replay records, demotion decisions, and cross-cycle Pattern-health trends all aggregate evidence across multiple cycles. Without `run_id`, those joins are fiction — you can't tell which cycle produced which record, and cross-cycle aggregation has no grouping key. The two existing sinks (`cascade_resolutions.jsonl`, `cost_log.jsonl`) omitted it during alpha.1 because alpha.1's per-cycle aggregation used an in-memory accumulator (ADR-0005), not a sink re-read.

## How

Additive field only. Old rows (pre-alpha.2) have no `run_id` and are simply uncorrelatable. No backfill, no schema break — the field is absent in old rows and present in new ones.
