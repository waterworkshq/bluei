# Model Governor lives at the LLM call site and ships with an identity default

The Model Governor owns only within-LLM-tier model selection — the deterministic-vs-LLM routing is already handled by the cascade (`reforge.py` fix-class taxonomy + `cascade.py` stages), so a second routing authority would duplicate it. The Governor is therefore a selection function inserted at the single LLM call site (`runner.py` `_template_for_backend` / `pr_cycle.py`'s claude invocation), not a new layer.

In alpha.6 the call site's default selection policy is **identity** (return the current model), so the Governor computes and records a recommendation per Finding but changes no live fix behavior. This mirrors the alpha.1 "observability first, no behavior change" pattern: the within-release consumer is the Benchmark Harness, which replays the synthetic corpus under experimental policies to measure effect. Flipping the default to act-on-recommendation is a beta.1 decision, gated on operators + live runs (ROADMAP line 29).

## Model identity is discovered at invocation, never hardcoded

The Governor emits only a **tier ordinal** (`tier-0` / `tier-1` / `tier-2` — an abstract cost/capability level) plus a rationale. It never knows model ids, rates, or CLI templates. Resolving a tier to a concrete model is the backend's job at invocation time: discover what the backend (`claude` / `opencode`) offers, let the backend's own metadata pick which discovered model satisfies the requested tier. We maintain no tier→model mapping table.

Rationale: model ids (`claude-sonnet-4`, `gpt-4o-mini`, …) are deprecated and renamed constantly by vendors; hardcoding them — in code or in a shipped config — ships stale-on-arrival. Discovery is the only source of truth that doesn't rot.

Tests mock discovery and invocation; CI never invokes real CLIs. Benchmark reproducibility comes from deterministic mocks, not from a stored tier→rate mapping. `MODEL_RATES` in `cost_tracker.py` stays as the legacy estimator for the existing Flywheel Ledger dollars-avoided arithmetic; the Governor does not touch it and introduces no new hardcoded rates.

## Considered Options

- **New full-ladder routing layer** (seed 08's literal framing). Rejected: duplicates the cascade's deterministic-vs-LLM logic and creates a second routing authority — the parallel-structure mistake.
- **Auto-downgrade live in alpha.6.** Rejected: a behavior change on a system with no operators to observe or correct regressions. Seed 08 itself flags "premature downgrade can increase failed fixes" and "operators need override controls."
- **Selection function only, defer call-site parameterization.** Rejected: leaves the integration entirely for beta.1 and makes the beta.1 flip an architectural change instead of a policy change.
- **Tier→model config table (`model_tiers.yaml`),** operator-overridable with shipped defaults. Rejected: shipped defaults still rot when vendors deprecate model ids; discovery is the only non-rotting source of truth.

## Consequences

- The call site gains a model-selection parameter in alpha.6; the parameter is exercised by the Benchmark (via mocks), not the live path.
- beta.1's "act on recommendation" is a default-policy flip, not an architectural change — provided the parameterization lands cleanly in alpha.6.
- The Governor must NOT be confused with Governance State (ADR-0008): a model choice is not a governance decision over a Governed Asset. They are independent axes.
- Tier→model resolution depends on backend discovery, which is environment-dependent; the Benchmark must pin discovery via mocks for reproducibility.
