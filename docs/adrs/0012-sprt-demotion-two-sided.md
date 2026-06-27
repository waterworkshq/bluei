# ADR-0012: Demotion and re-promotion use two-sided SPRT; demote sets Governance State → PAUSED only

**Status:** accepted (defaults theoretically grounded; empirically refinable from self-seeded data per seed 13)

Demotion and re-promotion of Governed Assets are decided by **Wald's Sequential Probability Ratio Test (SPRT)** on the Dry Replay outcome stream, replacing hardcoded count/rate thresholds. Demotion sets Governance State → PAUSED for all asset classes — no native lifecycle mutation, no confidence cap. Recovery is automatic.

## Why SPRT, not thresholds

Hardcoded thresholds ("≥3 FAILs / last 10 / ≥40%") are a particular snapshot of one configuration. They present themselves as the tuning surface when the actual tuning surface should be **risk tolerances**. SPRT exposes operator-facing inputs that mean something safety-related, and the count/rate behavior emerges from them as a derived instance.

## The algorithm

After each Dry Replay outcome `xᵢ` (1 = HIT, 0 = FAIL; MISS contributes nothing — a MISS is "Pattern didn't apply," not evidence about reliability), accumulate the log-likelihood ratio testing H₀ "Pattern healthy" against H₁ "Pattern broken":

```
LLR = Σᵢ [ xᵢ · ln(p₁/p₀) + (1−xᵢ) · ln((1−p₁)/(1−p₀)) ]

A = +ln((1−β)/α)   # demote boundary  (≈ +4.60 at defaults)
B =  +ln(β/(1−α))  # keep boundary    (≈ −4.60 at defaults)

LLR ≥ A → demote; reset LLR to 0
LLR ≤ B → keep;   reset LLR to 0
B < LLR < A → continue observing
```

Re-promote uses the **same test, two-sided**: after demotion, Dry Replay continues (ADR-0011 — Dry Replay does not consult Governance State), LLR keeps updating, and the next crossing of `A` re-promotes. The band between `B` and `A` (~9.2 LLR units at defaults) is **structural hysteresis** — no separate re-promote rule or its own magic numbers.

**Boundary semantics** (corrected 2026-06-28 code-review C2):
- `LLR ≥ A` → **auto_promote** (evidence strongly favors H₁ "healthy" — many HITs, positive LLR)
- `LLR ≤ B` → **auto_demote** (evidence strongly favors H₀ "broken" — many FAILs, negative LLR)

## Operator-facing inputs (the only knobs)

| Input | Meaning | Default |
|---|---|---|
| `p₁` | "Healthy" success rate | 0.90 |
| `p₀` | "Broken" success rate | 0.50 |
| `α` | Acceptable false-demotion rate | 0.01 |
| `β` | Acceptable false-keep rate | 0.01 |

`p₁`/`p₀` are per asset-class and per rule-family overridable. `α`/`β` are universal risk knobs. Operators tune *how sure* they want to be, not *how many failures*.

## Considered Options

- **(A) Statistical process control (p-chart).** Rejected — needs warmup baseline; "k=3 sigma" is itself magic one level removed; less sample-efficient.
- **(B) Beta-Bernoulli Bayesian posterior.** Rejected — `P(reliability < t) > c` still has magic `t` and `c`; posterior harder to read off an audit trail than a yes/no boundary crossing; less sample-efficient.
- **(C) Two-sided SPRT.** ✅ chosen. Sample-optimal (Wald's theorem); cold-start safe (LLR=0 sits in indifference zone); auditable from the record; hysteresis falls out structurally.

## What demote does

**Governance State → PAUSED, for all four asset classes, full stop.**

No native confidence cap, no lifecycle mutation. ADR-0011 already specifies that Dry Replay continues on PAUSED Patterns (the evidence stream is the recovery mechanism), so freezing evidence is unnecessary. The cascade-stage consultation layer (ADR-0008) skips PAUSED assets, preventing live breakage. SPRT continues running; the Pattern auto-re-promotes when LLR next crosses `B`.

The earlier proposal (cap Pattern confidence below `PROMPT_HINT=0.49` on demote) is **rejected** — it was solving an evidence-freeze problem that ADR-0011 already solves by continuing Dry Replay on PAUSED assets.

## LLR state persistence

The running LLR is **recomputed, not stored**. At each SPRT decision point:

1. Find the most recent ApprovalRecord for this Pattern with `decision` in `{auto_demote, auto_promote, resume}` — this is the reset boundary (ADR-0008: ApprovalRecord trail is the governance store).
2. Scan `dry_replay.jsonl` for this Pattern's outcomes since that timestamp.
3. Sum the log-likelihoods. Compare to A/B boundaries.

Crash-safe by construction — no stored accumulator to corrupt. The LLR is a pure function of two durable append-only stores. Record count per Pattern since last reset is bounded by the Dry Replay cap (≤20/cycle), so the scan is sub-millisecond even after months of operation.

## Consequences

- Audit trail entry shape: `"Demoted: LLR=4.61 crossed A=4.60 after 7 FAIL / 3 HIT (α=0.01, β=0.01, p₀=0.50, p₁=0.90)."` — any operator can recompute LLR from the stored outcome stream and verify the crossing.
- ADR-0011's forward reference to "ADR-0012 on demotion" (Dry Replay continues on PAUSED Patterns) is fulfilled here.
- **Indifference zone is a feature.** A Pattern whose true reliability sits between `p₀` and `p₁` (e.g., 70%) may never decide — LLR wanders between `B` and `A`. bluei does not want to demote such Patterns; it wants to keep watching. A max-sample cap can be layered on later if real repos show decision-starvation, but is deferred.
- Per-asset-class `p₀`/`p₁` defaults may differ: AST transforms (authored code, lower variance) might use `p₀=0.30`/`p₁=0.85`; Emergent Rules (whose reliability depends on shadow evidence quality) might use `p₀=0.40`/`p₁=0.80`. Defaults ship conservative; tuning is per-rule-family evidence work, not alpha.2 scope.
- The entire demotion substrate is asset-generic through the overlay (ADR-0008): SPRT runs on whatever Dry Replay outcome stream exists for the asset. In alpha.2 that's Patterns only; AST transforms gain Dry Replay in alpha.3 and inherit SPRT for free.
