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
| v0.2.0-beta.1 | "Stabilization (Act-on-Recommendation)": Governor flips from record-only to act-on-recommendation — `selection_fn` default `identity_selection` → `select_tier` (real tier recommendations); recommended tier resolved to a concrete model via a new `engine/model_discovery.py` (`BackendModelDiscovery`, operator-config-validated against the claude/opencode CLI; no shipped defaults) and injected into the command template at the call site via shared `resolve_governed_model` helper. Behavior change gated on operator `model_tiers.yaml` presence — under C2 (no operators) invocation is byte-identical to alpha.6. Batch path (`_apply_single_fix`) wired symmetrically (T1.2 deferral); cycle-start constructs discovery. `apply_claude_fix` unchanged. CR-3/4/5 test-quality hardening. ADR-0022 amended (2 amendments); 1 new CONTEXT term (Model Discovery). 6616 tests. C1 holds — real-repo measurement at rc.1. |

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
| CR-1 — STRUCTURAL subtree-walk recursion guard | **patch** | `_iter_structural_subtrees`'s recursive `_walk_body` is bounded by a 200-subtree count cap, NOT by depth. Adversarially-deep nesting (hundreds of nested `def`/`class` with one statement each) could recurse past Python's limit → `RecursionError`; `scan_shadow_rules` does not wrap `_scan_structural` in a try, so the error propagates. Normal code unaffected. Fix: add a depth parameter (`if depth > 64: return`) to `_walk_body`, OR wrap the call in `try/except RecursionError → []`. Apply to BOTH the runtime helper AND the generator template (keep in sync). | `bluei/app/emergent_rules.py` + `plugins/graduated-eslint/plugin.py:64` + `bluei/tools/graduator/templates.py:212` |
| CR-2 — campaign_evidence double load | **patch** | `store.load()` is called twice back-to-back (once to build `existing_ids`, once inside `store.save(store.load() + appended)`). Not a bug (nothing mutates between them), just a redundant file read + JSON parse on every campaign emergent-evidence pass. Fix: `current = store.load(); existing_ids = {r.rule_id for r in current}; ...; store.save(current + appended)`. | `bluei/app/campaign_evidence.py:100,103` |

---

### Deferred from v0.2.0-alpha.6 (DELIVERED)

alpha.6 shipped (see Delivered above). Items scoped out during grilling (Phase 2) + execution. The beta.1 constraint interrogation (Phase 1, 2026-07-05) confirmed C1 ("no live runs before stable") still holds for beta.1 — so items needing live data re-defer to rc.1 (the first live-runs release), while the flip, real discovery, and batch-path wiring land in beta.1 (proven via synthetic/mocked-subprocess tests, not live invocation).

| Deferred item | Target | Why deferred | Recorded in |
|---------------|--------|--------------|-------------|
| Act-on-recommendation (live default flip identity → select_tier) | **beta.1** | C3 operator-blind; a behavior change on a system with no operators. alpha.6 parameterizes the call site so the flip is a config change. | ADR-0022; PRD AC-P1-5 |
| Real model discovery (non-mocked tier→model resolution) | **beta.1** | Needs invocation; alpha.6 ships `MockModelDiscovery` only. | ADR-0022 |
| Validation-stability measurement in Benchmark | **rc.1** (re-deferred from beta.1) | Needs real model invocation to compare tier-0 vs tier-2 validation outcomes; C1 excludes live invocation until rc.1. | PRD AC-P2 risks; ADR-0022 |
| Validation-failure-history routing signal | **rc.1** (re-deferred from beta.1) | C1 — no committed per-family aggregation; needs live data, which rc.1's real-repo runs produce. | PRD Q4; ADR-0022 |
| File-criticality routing signal | **rc.1** (re-deferred from beta.1) | C1 — needs live repo context, which rc.1's real-repo runs provide. | PRD Q4; ADR-0022 |
| Real API token metering | **post-stable** | C2 — estimates kept; benchmark is relative. | ADR-0022 |
| T1.2 batch-path Governor wiring | **patch** | `batch_execution._apply_single_fix` calls `apply_claude_fix` but receives no cycle context (`ctx`) — so `selection_fn` + `governor_ledger_path` aren't accessible. Rather than force-fit a ctx parameter through the batch call chain, the pr-cycle path was wired alone. Fix when batch mode is prioritized: thread an optional `selection_fn` + ledger path through `apply_batch_fixes` → `_apply_single_fix`. | `bluei/engine/batch_execution.py:_apply_single_fix` |
| CR-3 — F3 proxy two-peer mutual-coverage test | **patch** | `tests/test_benchmark.py:test_f3_proxy_excludes_self_for_patterns` docstring claims it covers "a family with TWO patterns: both cascade_matched=True" but the body only asserts the single-pattern self-exclusion half (`deterministic_resolved == 0`). The mutual-coverage case (two patterns → both `cascade_matched` → `det == 2`) is the load-bearing correctness claim of the `- 1` self-exclusion and has no direct assertion. Fix: add a two-pattern manifest variant asserting `gap.deterministic_resolved == 2`. | `tests/test_benchmark.py` (~line 250) |
| CR-4 — governor_ledger_path=None no-op guard test | **patch** | The pr_cycle recording guard (`if ctx.governor_ledger_path is not None and ctx.selection_fn is not None:`) is the mechanism keeping the Governor inert when unconfigured. Every wiring test passes a real `ledger_path`, so no test exercises the guard's skip branch. Fix: add a test constructing `RunContext(governor_ledger_path=None)`, running the recording flow, asserting no file is created + no exception. | `bluei/engine/commands/pr_cycle.py:880-883`; `tests/test_governor_wiring.py` |
| CR-5 — cascade-resolved→no-Governor-row test | **patch** | The Governor recording block lives inside the `else` (LLM-fallback) branch of `_process_one_issue`, so cascade-resolved findings correctly get no recommendation row. No test pins this — wiring tests call recording directly, bypassing the branch structure. A regression moving recording above the `if/else` (recording for every finding including cascade wins) would go undetected. Fix: add an integration-style test asserting cascade-resolved → 0 ledger rows, LLM-routed → 1 row. | `bluei/engine/commands/pr_cycle.py:831` (the `else:` branch) |

---

### Deferred from v0.2.0-beta.1 (DELIVERED)

beta.1 shipped (see Delivered above). The beta phase is a long experimentation lifecycle; these items are tracked for future beta releases or rc. **Live-data-dependent items wait for rc** (gated on accepted live-repo dispatch — a long way off); **synthetic-provable + code-quality items are candidates for future betas** (beta.2+).

| Deferred item | Target | Why deferred | Recorded in |
|---------------|--------|--------------|-------------|
| **Cost-ledger ignores discovered pricing (M1)** | **beta.2+** (code-review-fix) | `record_invocation` looks up hardcoded `MODEL_RATES`; once a downgraded model_id flows it may charge the wrong rate, erasing the savings signal. Latent under C2 (no downgrades today). Fix: thread `ResolvedModel` rates into `record_invocation`. | Phase 8 review; `REMAINING-WORK.md` beta.1-CR-1 |
| **`model_id` format-string sensitivity (m2)** | **beta.2+** (patch) | `inject_model_flag` runs before `template.format()`; a `{`/`}` in a model_id could mis-substitute. Operator-trusted + `shlex.quote`'d; low risk. Robust fix touches `apply_claude_fix` (must stay unchanged) — defer. | Phase 8 review; `REMAINING-WORK.md` beta.1-CR-2 |
| **Batch-path guard asymmetry (m3)** | **beta.2+** (patch) | Local `if governor_ledger_path is not None` guard vs the helper's `selection_fn is None` guard — misleading belt-and-suspenders. Drop the local guard or check both. | Phase 8 review; `REMAINING-WORK.md` beta.1-CR-3 |
| **opencode discovery is a stub** | **beta.N** (when prioritized) | `_list_backend_models("opencode")` returns `{}` (no standard listing command today); `cli.py` loads only the `claude` tier_config. opencode downgrades are identity even with operator config. | Phase 5 research thread; `model_discovery.py` docstring |
| **Developer-doc audit beyond ARCHITECTURE.md** | **beta.2+** (patch) | `ARCHITECTURE.md` model_governor/model_discovery rows updated for beta.1; other developer docs (FIX_PIPELINE, etc.) may reference the Governor tangentially — sweep when those docs are next edited. | this table |
| Validation-stability measurement in Benchmark | **beta** (live-repo experimentation) | Needs real model invocation to compare tier-0 vs tier-2 outcomes; lands when beta's experimentation reaches live-repo dispatch. | PRD AC-P2 risks; ADR-0022 |
| Validation-failure-history routing signal | **beta** (live-repo experimentation) | Needs live per-family aggregation data; lands when beta experimentation runs on a live repo. | PRD Q4; ADR-0022 |
| File-criticality routing signal | **beta** (live-repo experimentation) | Needs live repo context; lands when beta experimentation runs on a live repo. | PRD Q4; ADR-0022 |
| Real API token metering | **beta** (when prioritized) | Estimates kept; benchmark is relative. | PRD C2 |
| Standalone `bluei savings` CLI | **beta** (whenever) | alpha.1 deferral; redundant with report/dashboard surface. | alpha.1 deferral |

> **Scoping rule (2026-07-05):** everything fixing, scoping, and feature-related lands in **beta**. rc has **no defined scope** — it is not a planned release with contents; it is a future maturity gate that will be scoped only when beta is mature enough to consider. Do not assign items to rc.

### Future beta work — code-review-and-fix sprints

beta.2 onward is expected to be **code-review-and-fix over the complete deterministic-flywheel feature** (the whole `alpha.1`→`beta.1` arc: Flywheel Ledger, Pattern Court, Governance substrate, Seed Library, Structural Replay, Recipe Foundry, Rule Hatchery, Taste Atlas, Campaign Lab, Model Governor, Benchmark Harness, Model Discovery). Each beta release should: (a) run fresh-context code reviews over a slice of the complete feature, (b) fix surfaced issues, (c) hardening. The beta.1 Phase 8 review (which caught the B1 silent no-op) is the template — that class of catch is the value of the beta lifecycle. beta ends when the feature is genuinely robust; only then is rc (live-repo dispatch) on the table.

---

### Toward 0.2.0 stable

After the flywheel arc lands (`alpha.1` through `alpha.6`), stabilization proceeds through the pre-release maturity ladder. **The beta phase is intentionally a long lifecycle** — beta is for experimentation and surfacing of issues against the complete deterministic-flywheel feature set, NOT a quick step to rc. Each beta release is expected to include code-review-and-fix sprints over the complete feature work. rc is gated on the beta being actively dispatched against a live production repo and accepted — that is a long way off.

| Step | Gate |
|------|------|
| `0.2.0-beta.1` ✅ | **Delivered.** Governor flipped to act-on-recommendation (`selection_fn` → `select_tier`, behavior gated on operator tier-config); real model discovery (operator-config-validated, mocked-subprocess proof). C1 held — the flywheel is "measurable" via the synthetic corpus + live routing logic in code. First stabilization gate; the feature set is complete in code, proven synthetically. |
| `0.2.0-beta.2..beta.N` | **The active lifecycle — everything fixing/scoping/feature lands here.** Each beta release: (a) code-review-and-fix sprints over the complete deterministic-flywheel feature (the whole alpha.1→beta.1 arc), (b) addressing surfaced issues + tracked deferrals, (c) feature work that becomes feasible as beta's experimentation matures (incl. live-repo experimentation on the owner's own production repo, which unblocks the live-data-dependent routing signals + measurement). Beta is the vehicle for finding and fixing what's wrong. No fixed end. |
| `0.2.0-rc.1` | **Scope not defined.** rc is a future maturity gate, not a planned release with contents. It will be scoped only when beta is mature enough to consider. **Do not assign items to rc** — everything fix/scoping/feature is beta work. |
| `0.2.0` (stable) | Docs match reality; no open schema-breaking decisions; the "do not use in production" warning can honestly come off. |

> **Re-scoped 2026-07-05 (beta.1 retrospective).** The earlier framing treated beta.1 → rc.1 as adjacent and gave rc a defined scope. Corrected: **everything fixing/scoping/feature lands in beta; rc has no defined scope.** The live-data-dependent features (validation-stability, validation-failure-history, file-criticality) are beta work — they land when beta's experimentation reaches live-repo dispatch, not in a pre-scoped rc.

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
