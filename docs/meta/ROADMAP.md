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
| v0.2.0-alpha.3 | "Evidence Foundation": Provenance-aware mining infrastructure (`open_pattern_store` factory, `register_asset` producer interface, provenance-by-storage-locus), envelope field completion (`imports_touched`/`validation_commands_passed`/`rule_family`), authoritative guidelines as prompt-context (ADR-0017), SPRT per-family calibration harness with hand-picked fallback. Seed Library packaging infrastructure (`ParsedRule` + `package.py` + `seeded_pattern_loader`) shipped; synthesize-then-validate source pipeline planned (ADR-0018). 6290 tests. |

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

### v0.2.0-alpha.4 - "Deterministic Assets" (was alpha.3)

Start turning repeated successful fixes into stronger deterministic assets. Now built on a *populated* substrate from alpha.3. **First release where the alpha.2 Operator Control Plane has consumers** — Recipe Foundry proposals flow through the approval gate, Rule Hatchery promotions get audit trails, AST replay transforms enter governance.

| Feature | Problem it solves |
|---------|-------------------|
| Recipe Foundry | Uses LLMs to propose deterministic Recipe YAML from successful fixes (including self-seeded candidates from alpha.3), then validates before activation |
| Rule Hatchery | Hardens repeated local smells into shadow-tested Emergent Rules |
| Python AST Structural Replay | Replays learned fix shapes across variable/import differences with dry-run and fallback behavior |

**Why here:** by this point the ledger, Pattern evidence, bundles, governance, AND a populated library exist. alpha.4 can safely make determinism stronger on top of real evidence.

**Consumes from alpha.3:** self-seeded Recipe candidates, `imports_touched` (for AST replay), `validation_commands_passed` (for Recipe Foundry validation), `rule_family` taxonomy, self-seeded structural shapes.

**Governance consumers activated:** Recipe Foundry promotion gate (first non-empty `bluei learn inbox`), Rule Hatchery lifecycle governance, AST transform activation governance. See seed `14-governance-consumers.md`.

Planning seeds: `docs/plans/v2/04-recipe-foundry.md`, `docs/plans/v2/05-rule-hatchery.md`, `docs/plans/v2/06-ast-structural-replay.md`, `docs/plans/v2/14-governance-consumers.md`

---

### v0.2.0-alpha.5 - "Repo Native" (was alpha.4)

Make bluei learn how each repo prefers code to be fixed.

| Feature | Problem it solves |
|---------|-------------------|
| Taste Atlas | Learns repo-local conventions: tests, fixtures, imports, naming, framework idioms, preferred libraries, and local exceptions |
| Campaign Lab | Turns Campaigns into evidence-gathering instruments, not just batch execution |

**Why here:** repo taste should influence learned fixes after the promotion pipeline exists. Campaigns can then intentionally collect evidence for deterministic assets.

Planning seeds: `docs/plans/v2/07-taste-atlas.md`, `docs/plans/v2/09-campaign-lab.md`

---

### v0.2.0-alpha.6 - "Economics" (was alpha.5)

Make the cost and reliability advantage measurable enough to become a buying argument.

| Feature | Problem it solves |
|---------|-------------------|
| Model Governor | Routes unresolved Findings to deterministic paths, smaller models, stronger models, frontier models, or humans based on evidence and risk. Reads Governance State as a first-class routing input (seed 14). |
| Benchmark And Proof Harness | Replays historical and synthetic Findings to prove fewer LLM calls, stable validation, and higher deterministic resolution rate. Reports governance lifecycle metrics from ApprovalRecord trail. |

**Why here:** model downgrade decisions need the evidence and promotion history from prior releases. v0.2.0-alpha.6 turns accumulated telemetry into economic proof.

Planning seeds: `docs/plans/v2/08-model-governor.md`, `docs/plans/v2/11-benchmark-proof-harness.md`

---

### Toward 0.2.0 stable

After the flywheel arc lands (`alpha.1` through `alpha.6`), stabilization proceeds through the pre-release maturity ladder:

| Step | Gate |
|------|------|
| `0.2.0-beta.1` | Feature-complete: Evidence → Economics shipped; deterministic flywheel measurable on internal repos |
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
| `skip` strategy asymmetry | Needs a small design decision before routing semantics are cleaned up |

*Updated 2026-06-25*
