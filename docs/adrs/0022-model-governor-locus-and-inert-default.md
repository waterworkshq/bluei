# Model Governor lives at the LLM call site and ships with an identity default

The Model Governor owns only within-LLM-tier model selection — the deterministic-vs-LLM routing is already handled by the cascade (`reforge.py` fix-class taxonomy + `cascade.py` stages), so a second routing authority would duplicate it. The Governor is therefore a selection function inserted at the LLM fix call sites — primarily `pr_cycle._process_one_issue` (beside the `apply_claude_fix` call), with the parallel `batch_execution._apply_single_fix` deferred (no ctx access). It is NOT inserted at `runner.py:_template_for_backend`, which is the scan/dry-run dispatch path, not the fix path (corrected during Phase 6 spec review). The selection function is injected via `RunContext.selection_fn` so beta.1's swap is a config flip, not a code edit.

In alpha.6 the call site's default selection policy is **identity** (return the current model), so the Governor computes and records a recommendation per Finding but changes no live fix behavior. This mirrors the alpha.1 "observability first, no behavior change" pattern: the within-release consumer is the Benchmark Harness, which replays the synthetic corpus under experimental policies to measure effect. Flipping the default to act-on-recommendation is a beta.1 decision (see "beta.1 realization" below — the flip landed; behavior is gated on operator tier-config presence, a finer gate than the coarse identity default).

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

## beta.1 realization (2026-07) — the flip + discovery contract

beta.1 fulfills the two decisions this ADR deferred ("beta.1 swaps identity → select_tier as a policy flip"; "tier→model resolution depends on backend discovery"). Recorded here as amendments, not a new ADR, to keep the Governor narrative in one place (mirrors the alpha.6 locus-correction amendment pattern).

### Amendment 1 — the flip: `selection_fn` default → `select_tier`; behavior gated on operator config presence

beta.1 flips `RunContext.selection_fn` default from `identity_selection` → `select_tier`. The Governor now records **real** tier recommendations per Finding (not the vacuous tier-2 from identity). This fulfills the ROADMAP beta.1 gate ("Governor default flipped to act-on-recommendation") literally.

The **behavior change** (actual model downgrade at invocation) is gated on **operator tier-config presence** — a finer gate than the coarse identity default. Under the project's C2 constraint (no operators yet), no `model_tiers.yaml` exists → every recommendation resolves to the default model → **no live behavior change**. Real downgrades only occur when an operator supplies a tier mapping. This resolves the apparent tension between the ROADMAP gate language ("default flipped") and this ADR's original "gated on operators + live runs" condition: the gate shifted from the selection function to operator-config presence, which is both more honest and more precise.

The resolution locus (tier → resolved model → command-template flag) is the **pr-cycle call site** (`_process_one_issue`, beside the `apply_claude_fix` call) — not inside `apply_claude_fix`. `apply_claude_fix` remains a pure "format template + run subprocess" function; its contract and 12+ callers are unchanged. The hardcoded `model_name = "claude-sonnet-4"` at the call site (cost-ledger estimator) is replaced by the discovered model id.

### Amendment 2 — discovery contract: operator-supplied tier mapping, discovery-validated, no shipped defaults

Tier→model resolution is **operator-config-driven, validated against backend discovery**:
- The operator supplies a tier→model mapping in config (`model_tiers.yaml`: `tier-0: <selector>, tier-1: ..., tier-2: ...`). **No shipped defaults** — so nothing rots when vendors deprecate model ids.
- Model Discovery queries the backend CLI (`claude` / `opencode`) to list actually-available models, then **validates** the operator's mapping (rejects mappings to unavailable models; falls back to the default model on mismatch).
- Under C2 (no operator config), every recommendation resolves to the default model — identity behavior, preserved.

**Rejected alternatives:** (A) derive tiers from backend cost/capability metadata — rejected because CLIs expose uneven/comparable metadata; brittle when a model lacks clean cost fields. (C) positional ranking by the CLI's native ordering — rejected because it assumes a stable, meaningful ordering that doesn't exist. Operator-config-validated is the only contract that (i) ships nothing that rots (ADR-0022's core rule), (ii) works under C2 (empty config = identity behavior), and (iii) doesn't depend on backend metadata quality.

**Discovery interface:** `discover(backend) -> List[ResolvedModel]` (the shape `MockModelDiscovery` already provides in the Benchmark). beta.1 adds a real implementation querying the CLIs, proven via mocked-subprocess tests (consistent with the alpha posture — real logic, synthetic proof). The discovery protocol + real implementation live in `bluei/engine/` (runtime path; layering forbids engine→tools). `MockModelDiscovery` stays in `bluei/tools/benchmark/` for Benchmark reproducibility.

**Open research thread (Phase 5):** the exact `claude`/`opencode` model-listing commands and their metadata shape — determines the discovery implementation, not the contract decision above.
