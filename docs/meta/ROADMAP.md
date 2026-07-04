<!-- CATEGORY: meta -->

# Roadmap — bluei

What's been built, and what's next.

---

## Delivered

| Ship | Story |
|------|-------|
| v0.1.0-alpha.1 | Initial pre-release: deterministic fix cascade, pattern learning, review lifecycle, onboarding, 8 language plugins |
| v0.1.0-beta.1 | Post-alpha hardening: Tier 1 architecture cleanup, god-module decompositions, H5 review→app decoupling, merge-cycle audit closure, JSONL/state reconciliation, E2E test suite |
| v0.2.0-alpha.1 | "Evidence": Flywheel Ledger (deterministic-vs-LLM resolution metrics, cascade-stage counts, honest three-way HIT/MISS/FAILURE replay outcomes, standalone-Pattern-replay $ savings) + Pattern Court Lite (`last_failed_at`/`excluded_paths` envelopes with lookup-layer enforcement, enriched `patterns show`/`list`, `exclude`/`unexclude` producer). Rendered across the `clean` summary, `report` (markdown + HTML), and dashboard. Observability only — no fix-behavior changes. |
| v0.2.0-alpha.2 | "Safe Learning": Governance substrate (Operator Control Plane with event-sourced ApprovalRecord trail, per-class policy-driven promotion gate, tri-state safe-mode), Golden Validation Bundles (container + storage + ruff-b904 fixture), Dry Replay (non-mutating evidence collection for matched-but-not-selected Patterns), SPRT demotion (two-sided Wald's Sequential Probability Ratio Test), `bluei learn` CLI namespace. 10 ADRs (0006–0016), 6245 tests. Ships inert — evidence population deferred to alpha.3 self-seeding. |
| v0.2.0-alpha.3 | "Evidence Foundation": Provenance-aware mining infrastructure (`open_pattern_store` factory, `register_asset` producer interface, provenance-by-storage-locus), envelope field completion (`imports_touched`/`validation_commands_passed`/`rule_family`), authoritative guidelines as prompt-context (ADR-0017), SPRT per-family calibration harness with hand-picked fallback. Synthesize-then-validate Seed Library pipeline (ADR-0018): LLM generates canonical rules, linter validates as oracle. 38 validated bundles shipped (30 Python + 7 JS + 1 original). 6295 tests. |
| v0.2.0-alpha.4 | "Deterministic Assets": Structural Replay (ADR-0019 — AST-aware Pattern application on literal miss with two-stage confidence gate), Recipe Foundry (ADR-0020 — build-time LLM-authoring pipeline with 37 canonical seeded-Recipes produced via first Foundry run), Rule Hatchery detection extraction (ADR-0021 — regex-based detection replacing the rule-name stub + rule-owned FP measurement). 38 seeded Patterns populated across 12 rule families. `bluei learn` governance decision verbs (ADR-0015 revised). eslint autofix wildcard. 4 ADRs (0019-0021 + 0007/0015 refined). 6401 tests. |
| v0.2.0-alpha.5 | "Repo Native": Taste Atlas (Repo Taste Profile — framework-matched prompt-context seed library mirroring Authoritative Guidelines, ADR-0017; injected as prompt §6f + surfaced in reports; NOT governed), Campaign Lab (Learning Objective + evidence routing via injected consumer — campaigns advance native lifecycle + write evidence stores only, ZERO governance ApprovalRecord writes; decoupled from app via callback injection), Plugin-skeleton generator (build-time `bluei/tools/graduator/` → self-contained `DiscoveryPlugin` packs; 1 curated worked example `plugins/graduated-eslint/`), AST STRUCTURAL detection (ADR-0021 fulfillment — `extract_detection_ast` + `scan_shadow_rules` STRUCTURAL branch, Python-only, exact hash + fuzzy fallback). Zero new ADRs (integration release). 6463 tests. |
| v0.2.0-alpha.6 | "Economics": Model Governor (ADR-0022 — tier-selection function at the LLM call site; emits tier-0/1/2/escalate based on rule-family deterministic coverage; ships inert with identity default returning tier-2; selection_fn injected via RunContext so beta.1's swap is a config flip; no hardcoded model ids — discovery-based resolution), Benchmark Harness (internal dev tool `bluei/tools/benchmark/` + `bluei/engine/corpus_manifest.py` — replays the 130-entry synthetic corpus through the cascade-simulation proxy + Governor; per-family coverage gap analysis + Flywheel Score via mocked rates; `bluei benchmark` CLI; static analysis only), user-facing "$ avoided" statistic (`routing_savings_usd` additive key in flywheel_ledger + aggregate render). 1 new ADR (0022). 4 new CONTEXT terms. 6552 tests. Ships inert — no live downgrades until beta.1. |

> Full release notes live in [`CHANGELOG.md`](../CHANGELOG.md).

---

## Upcoming

Each minor release should tell one coherent story. The `0.2.0` line is the deterministic flywheel era: each release increments the alpha counter through the flywheel build-out, aiming for `0.2.0-beta` → `0.2.0-rc` → `0.2.0` stable once the arc is feature-complete and proven on real repos. Release boundaries are risk-management decisions: substrate, evidence population, structural replay, repo taste, and model routing should not all ship in one batch.

> **Arc restructured 2026-06-27** (alpha.2 grilling). Original plan had alpha.3="Deterministic Assets" absorbing alpha.2 deferrals directly. Two corrections: (1) inserting alpha.3 "Evidence Foundation" between substrate and exploitation prevents scope snowball; (2) the system will not run on any live or simulated repo before 0.2.0 stable, so evidence must be self-seeded from open-source vetted data, not grown from production fixes. Original alpha.3→alpha.4, alpha.4→alpha.5, alpha.5→alpha.6.

### Carried forward from v0.2.0-alpha.1

v0.2.0-alpha.1 "Evidence" has shipped (see Delivered above). These items were scoped out of that release and are inherited by later releases (mostly alpha.2):

| Deferred item | Target | Why deferred |
|---------------|--------|--------------|
| Shadow (Dry) Replay | alpha.2 "Safe Learning" | The safe-learning instrument itself; needs non-mutating replay path + would-have-applied store + duplicate-validation handling. alpha.1 only observes; alpha.2 acts. |
| Demotion / promotion triggers | alpha.2 "Safe Learning" | Acts on FAILURE evidence alpha.1 begins collecting. Auto-confidence changes are a behavior change, excluded from the "observability first" release. |
| Envelope fields: `imports_touched`, `validation_commands_passed`, negative-example/blocked-contexts store, `rule_family` taxonomy | alpha.2 | Each needs new capture infra (imports visitors currently *discard* module names; `_tier_validate` returns only bool+message; no rule taxonomy exists). alpha.1 ships only the cheaply-derivable fields. |
| `bluei savings <repo>` standalone CLI | post-alpha.1 (patch or later) | Redundant with the enriched `clean` summary + `report` section + dashboard card, which fully satisfy seed-01 acceptance. Cumulative ledger JSONL makes a read-only CLI trivial to add once operators ask for it. |
| Cascade-internal Pattern/composite savings instrumentation (thread `cost_tracker` into `CascadeContext`) | alpha.2 | Broader plumbing change excluded from additive-only alpha.1; revives deferred `bluei savings` completeness and makes cascade-internal Pattern/composite wins contribute real $ instead of counts-only. Captured in ADR-0003 (option C). |
| `run_id` correlation key on ledger/cost sinks | when cumulative/cross-cycle Ledger features begin (alpha.2 / `bluei savings`) | alpha.1's per-cycle block reads an in-memory accumulator (ADR-0005), so no `run_id` is needed; `cascade_resolutions.jsonl` + `cost_log.jsonl` rows omit it. The first cumulative/cross-cycle view (per-cycle savings CLI, cross-cycle trends, shadow-replay history) requires `run_id` generated in the engine CLI and stamped on both sinks, then read-time join. Add before starting any such feature. |

---

### Deferred from v0.2.0-alpha.5 (DELIVERED)

alpha.5 shipped (see Delivered above). Items scoped out during grilling (Phase 4 arc review). Each has a target release/patch and is recorded in the cited seed doc so the next release's Phase 0 picks it up.

| Deferred item | Target | Why deferred | Recorded in |
|---------------|--------|--------------|-------------|
| Taste injection into the 4 specialized prompts (`prompts.py:304-341` take zero context channels today) | **patch** (whenever prompts touched) | Focused refactor; no consuming feature. Also benefits Authoritative Guidelines / patterns / history. Must not be orphaned. | seed 07 |
| TS/JS AST STRUCTURAL detection + TS structural hasher | **future-cross-cutting** (post-alpha.6) | alpha.5 STRUCTURAL is Python-only (mature AST hasher); TS has none. TS hasher is standalone work; not a fit for alpha.6 "Economics." Regex (ADR-0021) covers TS meanwhile. | seed 05; ROADMAP Future Cross-Cutting Seeds |
| Governed Repo Taste Profile (5th governed asset class) | **post-stable** | Trigger: repo-observed taste exists (live runs). alpha.5 taste is prompt-context (influences, doesn't auto-apply). | seed 07; seed 14 |
| Campaign Lab as a governance ApprovalRecord producer | **post-stable** (conditional) | Only if a campaign must *decide* governance. alpha.5 campaigns are evidence sources (native lifecycle only), never governance decision-makers. | seed 09; seed 14 |
| Campaign auto-feed *real* Pattern Court promotion under live runs | **post-stable** | Routing wiring lands alpha.5 (synthetic-proven); real firing needs live campaigns. | seed 09 |
| Shadow-evidence-driven cluster→candidate automation | **post-stable** | Originally targeted alpha.5 (seed 05) but outside the 4-feature scope; needs live shadow evidence. | seed 05; seed 09 |
| "Rank Findings by deterministic potential" + batch-size auto-tuning | **post-stable** | Need live run telemetry. alpha.5 routes by declared objective but does not rank or auto-tune. | seed 09 |
| Live-graduated plugin rule governance | **post-stable** | alpha.5 ships only pre-vetted build-time plugins (seed-library pattern); live graduation governance is post-stable. | seed 05; seed 14 |

---

### Deferred from v0.2.0-alpha.6 (DELIVERED)

alpha.6 shipped (see Delivered above). Items scoped out during grilling (Phase 2) + execution. Each targets `0.2.0-beta.1` (the stabilization gate) and is recorded in the PRD out-of-scope + ADR-0022.

| Deferred item | Target | Why deferred | Recorded in |
|---------------|--------|--------------|-------------|
| Act-on-recommendation (live default flip identity → select_tier) | **beta.1** | C3 operator-blind; a behavior change on a system with no operators. alpha.6 parameterizes the call site so the flip is a config change. | ADR-0022; PRD AC-P1-5 |
| Real model discovery (non-mocked tier→model resolution) | **beta.1** | Needs invocation; alpha.6 ships `MockModelDiscovery` only. | ADR-0022 |
| Validation-stability measurement in Benchmark | **beta.1** | Needs invocation to compare tier-0 vs tier-2 validation outcomes. | PRD AC-P2 risks |
| Validation-failure-history routing signal | **beta.1** | C1 — no committed per-family aggregation; needs live data. | PRD Q4 |
| File-criticality routing signal | **beta.1** | C1 — needs live repo context. | PRD Q4 |
| Real API token metering | **post-stable** | C2 — estimates kept; benchmark is relative. | PRD C2 |
| T1.2 batch-path Governor wiring | **patch** | `_apply_single_fix` has no ctx; deferred rather than force-fit. | TASKS T1.2 |
| CR-3/4/5 test-quality hardening (F3 two-peer, ledger-None guard, cascade-resolved→no-row) | **patch** | LOW; contracts currently work, tests missing. | `docs/plans/REMAINING-WORK.md` |

---

### Toward 0.2.0 stable

After the flywheel arc lands (`alpha.1` through `alpha.6`), stabilization proceeds through the pre-release maturity ladder:

| Step | Gate |
|------|------|
| `0.2.0-beta.1` | Feature-complete: Economics shipped (alpha.6 delivered); Governor default flipped to act-on-recommendation; deterministic flywheel measurable on internal repos |
| `0.2.0-rc.1` | Proven on 2-3 repos you do not own; no data-loss/safety footguns; CLI/config/state schema frozen |
| `0.2.0` (stable) | Docs match reality; no open schema-breaking decisions; the "do not use in production" warning can honestly come off |

---

### Future Cross-Cutting Seeds

These remain unscheduled until prerequisite foundations are in place.

| Seed | Why it waits |
|------|--------------|
| Competitor positioning (`docs/plans/v2/12-competitor-positioning.md`) | Needs primary-source validation before public claims |
| Mnemo cross-finding recall | `_get_mnemo_client` exists in `lifecycle.py` but local deterministic systems are higher priority first |
| `engine/report.py` integration roadmap | Useful after Flywheel Ledger decides which report surfaces need engine data |
| `bluei notify config` wizard | Operational polish; not part of the deterministic flywheel path |
| FixPatternStore O(n) rebuild | Becomes urgent when Pattern evidence and replay volume grow; options remain append-only compaction, sqlite, or in-memory flush |
| Multi-file composite rollback | Should be revisited before composite Patterns are promoted aggressively |
| Non-Python AST cascade | Defer until Python AST structural replay proves the design |
| TS/JS AST STRUCTURAL detection + TS structural hasher | Deferred from alpha.5 (Python-only STRUCTURAL shipped). Needs a TS AST hasher (tree-sitter/regex-AST); standalone work. Regex detection (ADR-0021) covers TS meanwhile. Pick up when TS emergent-rule detection is prioritized. |
| campaigns→app layering: `planner.py:437` `import_graph` import | Pre-existing since alpha.1 (`90d6f22`); MEMORY's "campaigns→app 0 current" was inaccurate. `enforce_architecture.py` doesn't check campaigns→app. Fix: move `build_import_graph` to engine OR decouple the `dependency_ordered` planner strategy. |
| `skip` strategy asymmetry | Needs a small design decision before routing semantics are cleaned up |

*Updated 2026-06-30*
