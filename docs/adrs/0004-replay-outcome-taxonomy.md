# ADR-0004: Replay outcomes are a three-way enum, recorded exactly once per Finding

**Status:** accepted

`record_replay(success: bool)` today conflates outcomes and is partly wrong: its `success=False` path is reached **only** via validation failure (`pattern_replay.py:283`) yet increments `skip_count`, so `skip_count` actually holds validation *failures*; true misses (`file_not_found`, `snippet_not_found`) call `record_replay` not at all; and `failure_count` is orphaned. We promote replay to a three-way `ReplayOutcome` — **HIT** (`success_count` + `last_verified_at`), **MISS** (`skip_count` + `last_used_at`, recorded at the previously-silent miss sites), **FAILURE** (`failure_count` + new `last_failed_at`) — with a backward-compat `bool` overload (`True→HIT`, `False→MISS`).

Separately, `try_replay` is invoked **twice** for cascade-fix Findings whose standalone replay fails: once by pr_cycle (`pr_cycle.py` ~707) and again by `PatternReplayCascadeStage.attempt` (`cascade.py:491`). Since `try_replay` is deterministic, the retry double-records the same MISS/FAILURE. We add `record_outcome: bool = True` to `try_replay`: the standalone path passes `record_outcome=replayed` (record only on HIT, the fast-path win that short-circuits the cascade), the cascade stage keeps the default (always record). Result: exactly-once recording per Finding with zero change to confidence dynamics or auto_tune.

## Considered Options

- **(A) Three-way enum + `record_outcome` deferral.** ✅ chosen.
- (B) Keep `bool`, split `skip_count` into two existing fields by overloading. Rejected: preserves the mislabel and the two-call double-record.
- (C) Move `record_replay` out of `try_replay` entirely. Rejected: larger refactor that risks confidence/auto_tune behavior change, violating additive-only.

## Consequences

- `failure_count` finally gets its real meaning; `last_failed_at` is a new optional additive field (old Patterns load unchanged).
- Pattern-replay HIT/MISS/FAILURE counts in the Ledger read three real, exactly-once counters.
- Court Lite's `patterns show` enrichment (ADR scope: alpha.1) renders the honest breakdown.
