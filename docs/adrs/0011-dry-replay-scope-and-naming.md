# ADR-0011: Dry Replay runs for matched-but-not-selected Patterns, per-cycle capped, negative-priority

**Status:** accepted

Dry Replay is the non-mutating evidence-collection path for learned Patterns: it applies a Pattern's fix to a scratch copy, runs the Validation Gate, records the would-have-applied outcome, and discards the scratch — without persisting the fix or affecting the live cycle.

**Naming:** "Dry Replay" (not "Shadow Replay"). `review/` already owns `LiveRolloutMode.SHADOW` (would-have-*published* a PR review comment — different subsystem). "Dry" mirrors the existing `--dry-run` convention in `bluei duster` and `bluei campaign run --dry-run`.

## Scope: matched-but-not-selected, with a cap

During a pr-cycle, for any given Finding, the Pattern cascade stage's `lookup()` returns candidates that fall into three buckets:

1. **Cascade winner** — replayed live; outcome recorded by alpha.1's HIT/MISS/FAILURE path. No Dry Replay.
2. **Matched-but-not-selected** — `lookup()` matched, but the cascade stopped at an earlier stage (Linter/Recipe won). Genuine "would-have-applied" population. Dry Replay runs here.
3. **Did-not-match** — `lookup()` returned None. Nothing to replay.

Dry Replay runs for **bucket (2) only**, **capped at N attempts per cycle** (default 20, configurable). When the cap is hit, remaining candidates are skipped. Candidates are prioritized by recency-of-last-dry-run (oldest first) so evidence accumulates fairly across the Pattern population. Every attempted Dry Replay records its outcome — the cap limits *attempts*, not *recordings*, because the outcome (HIT/MISS/FAILURE) is unknown until after the expensive validation runs. SPRT's asymmetric weighting (FAIL contributes ~2.7× more to LLR than HIT at default p₀/p₁) already ensures FAILs drive demotion faster — no special "negative-priority bypass" is needed.

## Scratch mechanism

Dry Replay cannot use the live worktree directly — by the time Dry Replay runs, the cascade winner has already mutated the target file. Applying a non-selected Pattern's fix on top of the winner's fix is meaningless. A separate worktree from the base commit has incorrect context (other files still have their original lint errors, producing false FAILs).

**Same worktree, file-level checkpoint:** before the cascade applies the winning fix to a Finding's target file, save the original content. After the winner is applied, for each Dry Replay candidate: restore original content → apply Pattern fix → run validation → record outcome → restore winner's fix. Validation runs against a worktree where all other fixes are in place (realistic context) and only the target file has the Pattern's fix instead of the winner's.

Crash risk (process dies between Pattern fix and winner restore) is the same category the cycle already accepts for every cascade fix application. bluei's dirty-tree protection catches it on the next cycle. For multi-file Patterns, checkpoint all affected files; most alpha.2 Patterns are single-file.

## Why this scope

- **(A) Every matched Pattern, every cycle** — rejected. Unbounded: a repo with 100 Findings and 30 Patterns could produce hundreds of Dry Replays per cycle, each running the full validation suite. Non-starter for cron deployments.
- **(B) Matched Patterns in a "shadow candidate" set only** — rejected. Misses the "established Pattern silently breaks after a refactor" case — the Pattern isn't a promotion candidate (already confident), so it's not in the shadow set, but it's now producing bad fixes. Only a broad-but-capped approach catches this.
- **(C) Matched-but-not-selected, capped** — ✅ chosen. Bounds cost, catches both promotion and demotion evidence.

## Record shape

`repos/<name>/state/dry_replay.jsonl`, append-only, one row per Dry Replay:

```
{
  "run_id": "<uuid>",            # ADR-0010
  "pattern_id": "fp-...",
  "finding_id": "f-...",
  "rule": "ruff-b904",
  "would_have_outcome": "HIT" | "MISS" | "FAILURE",
  "validation_commands_passed": [],   # populated in alpha.3 (ADR-0016 defers capture infra)
  "timestamp": "<ISO>"
}
```

Cumulative reads (promotion/demotion aggregation) join on `pattern_id` across `run_id`s. `run_id` is mandatory (ADR-0010).

## Consequences

- Dry Replay is **Pattern-only in alpha.2** (only Patterns replay today). The record shape is asset-generic so AST transforms can plug in during alpha.3 once they have a replay path.
- Dry Replay is **not a cascade stage** — it runs alongside the cascade as an evidence collector. It does not affect live fix outcomes and does not consult the Governance State of the Pattern (a PAUSED Pattern's would-have-applied outcome is still valuable evidence — see ADR-0012 on demotion).
- The cap-hit count surfaces in the Flywheel Ledger dashboard card so operators know evidence is being rate-limited.
- Dry Replay cost is bounded but non-zero: each run applies a fix + runs validation on a scratch copy. The cap is the safety valve; operators on large repos may raise it via config.
- Naming locked: "Dry Replay" enters the glossary; "Shadow Replay" is explicitly rejected to avoid review/ collision.
