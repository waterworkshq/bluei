# ADR-0003: Dollar savings are counted only for observed standalone Pattern replays

**Status:** accepted

The Ledger reports real dollar savings **only** for standalone Pattern replays — the one path where a learned fix literally stood in for an LLM call and where savings are actually recorded today (`pr_cycle.py:731-736`, via `CostTracker.estimate_invocation_cost` + `record_pattern_replay_savings`). All other deterministic resolutions — Linter, Recipe, AST, *and cascade-internal Pattern / composite-Pattern replays* — are reported as **counts and rates only**, with no dollar figure (the column reads "—"). This keeps every published dollar number grounded in an observed, recorded substitution rather than a counterfactual.

## Why cascade-internal Pattern/composite are NOT in the $ bucket

Verified: `record_pattern_replay_savings` is called **only** in the standalone `try_replay` success branch of pr_cycle. Neither `PatternReplayCascadeStage` nor `CompositePatternCascadeStage` calls it (they use `try_replay`/`CompositePatternApplier` + `record_result`, never the cost tracker). So when standalone replay fails and a cascade-internal Pattern/composite stage then succeeds, no savings row is written — that win is genuinely $0 in recorded data. Counting it as $ would require either inventing a counterfactual (rejected) or threading `cost_tracker` into `CascadeContext` to record it (a broader change deferred out of the additive-only release).

## Considered Options

- **(A) $ for standalone Pattern replay only; everything else counts-only.** ✅ chosen — matches today's instrumentation exactly, zero new cost code, no counterfactual.
- (B) Counterfactual $ for all deterministic wins. Rejected: unverifiable token envelopes — the seed's "savings can look fake" risk.
- (C) Thread `cost_tracker` into the cascade to record Pattern/composite savings. Rejected for alpha.1: broader plumbing change; deferred — tracked as a candidate alpha.2 enhancement once the cascade has cost awareness.

## Consequences

- The token-envelope assumption stays confined to standalone replay, where the substitution is observed and recorded.
- A footnote on every savings surface states: *dollar savings reflect standalone Pattern-replay substitutions; cascade-internal Pattern/composite resolutions and built-in deterministic stages are reported as throughput, not cost.*
- The deferred `bluei savings` CLI and any cumulative view inherit this same boundary until (C) is pursued.
