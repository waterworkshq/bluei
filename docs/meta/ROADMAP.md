<!-- CATEGORY: meta -->

# Roadmap — bluei

What's been built, and what's next.

---

## Delivered

| Ship | Story |
|------|-------|
| v0.1.0-alpha.1 | Initial pre-release: deterministic fix cascade, pattern learning, review lifecycle, onboarding, 8 language plugins |
| v0.1.0-beta.1 | Post-alpha hardening: Tier 1 architecture cleanup, god-module decompositions, H5 review→app decoupling, merge-cycle audit closure, JSONL/state reconciliation, E2E test suite |

> Full release notes live in [`CHANGELOG.md`](../CHANGELOG.md).

---

## Upcoming

Each minor release should tell one coherent story. The `0.2.0` line is the deterministic flywheel era: each release increments the alpha counter through the flywheel build-out, aiming for `0.2.0-beta` → `0.2.0-rc` → `0.2.0` stable once the arc is feature-complete and proven on real repos. Release boundaries are risk-management decisions: evidence, promotion, structural replay, repo taste, and model routing should not all ship in one batch.

### v0.2.0-alpha.1 - "Evidence"

Make the deterministic flywheel visible before changing automation behavior.

| Feature | Problem it solves |
|---------|-------------------|
| Flywheel Ledger | Shows deterministic-vs-LLM resolution rate, cascade-stage counts, Pattern replay outcomes, and estimated token/dollar savings |
| Pattern Court Lite | Adds minimal Pattern evidence envelopes and replay explanations so learned fixes are inspectable |

**Why here:** bluei already has deterministic stages, Pattern replay, and cost tracking, but the value is buried. v0.2.0-alpha.1 should prove the thesis with observability first, not introduce risky auto-promotion.

Planning seeds: `docs/plans/v2/01-flywheel-ledger.md`, `docs/plans/v2/02-pattern-court.md`

---

### v0.2.0-alpha.2 - "Safe Learning"

Add the safety substrate required before learned fixes can promote themselves.

| Feature | Problem it solves |
|---------|-------------------|
| Golden Reef | Gives Patterns, Recipes, transforms, and Emergent Rules Golden Validation Bundles instead of relying only on confidence scores |
| Operator Control Plane | Lets operators approve, reject, pause, demote, or retire learned automation with an audit trail |
| Pattern Shadow Replay | Records would-have-applied Pattern outcomes without mutating code |

**Why here:** promotion without negative examples, fixtures, and operator governance is too opaque. This release makes learning safe before making it more powerful.

Planning seeds: `docs/plans/v2/03-golden-reef.md`, `docs/plans/v2/10-operator-control-plane.md`, `docs/plans/v2/02-pattern-court.md`

---

### v0.2.0-alpha.3 - "Deterministic Assets"

Start turning repeated successful fixes into stronger deterministic assets.

| Feature | Problem it solves |
|---------|-------------------|
| Recipe Foundry | Uses LLMs to propose deterministic Recipe YAML from successful fixes, then validates before activation |
| Rule Hatchery | Hardens repeated local smells into shadow-tested Emergent Rules |
| Python AST Structural Replay | Replays learned fix shapes across variable/import differences with dry-run and fallback behavior |

**Why here:** by this point the ledger, Pattern evidence, bundles, and governance exist. v0.2.0-alpha.3 can safely make determinism stronger.

Planning seeds: `docs/plans/v2/04-recipe-foundry.md`, `docs/plans/v2/05-rule-hatchery.md`, `docs/plans/v2/06-ast-structural-replay.md`

---

### v0.2.0-alpha.4 - "Repo Native"

Make bluei learn how each repo prefers code to be fixed.

| Feature | Problem it solves |
|---------|-------------------|
| Taste Atlas | Learns repo-local conventions: tests, fixtures, imports, naming, framework idioms, preferred libraries, and local exceptions |
| Campaign Lab | Turns Campaigns into evidence-gathering instruments, not just batch execution |

**Why here:** repo taste should influence learned fixes after the promotion pipeline exists. Campaigns can then intentionally collect evidence for deterministic assets.

Planning seeds: `docs/plans/v2/07-taste-atlas.md`, `docs/plans/v2/09-campaign-lab.md`

---

### v0.2.0-alpha.5 - "Economics"

Make the cost and reliability advantage measurable enough to become a buying argument.

| Feature | Problem it solves |
|---------|-------------------|
| Model Governor | Routes unresolved Findings to deterministic paths, smaller models, stronger models, frontier models, or humans based on evidence and risk |
| Benchmark And Proof Harness | Replays historical and synthetic Findings to prove fewer LLM calls, stable validation, and higher deterministic resolution rate |

**Why here:** model downgrade decisions need the evidence and promotion history from prior releases. v0.2.0-alpha.5 turns accumulated telemetry into economic proof.

Planning seeds: `docs/plans/v2/08-model-governor.md`, `docs/plans/v2/11-benchmark-proof-harness.md`

---

### Toward 0.2.0 stable

After the flywheel arc lands (`alpha.1` through `alpha.5`), stabilization proceeds through the pre-release maturity ladder:

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
