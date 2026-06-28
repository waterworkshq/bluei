# bluei — Domain Context

The shared vocabulary for the bluei QA agent codebase. Architectural discussions, code review, and planning documents should use these terms exactly.

## Language

**Finding**
A detected issue in a target repo — lint error, type violation, style problem, test gap, or any rule-triggered observation. Persisted in `findings.jsonl`.
_Avoid_: issue (reserved for the GitHub issue tracking wrapper), defect, bug, error.

**Issue**
A tracked GitHub issue created from one or more Findings. Wraps a Finding with lifecycle state (status, history, escalation tracking). Persisted in `issues.json`.
_Avoid_: ticket, finding (a Finding becomes an Issue, but they are distinct).

**Cycle**
A single run phase. Five types: issue-cycle (discovery), pr-cycle (fix + PR), merge-cycle (auto-merge), orchestrated (full pipeline), review-cycle (PR review).
_Avoid_: run (ambiguous — a run may contain multiple cycles), pass, sweep.

**Care Level**
How autonomous bluei can be for a repo. Four tiers: watch-only, note-only, offer-fixes, full-care.
_Avoid_: mode (too generic), autonomy level, trust level.

**Pattern**
A learned fix extracted from a successful diff. Stored with confidence scores and structural hashes. Replayed deterministically on future matching Findings.
_Avoid_: template (reserved for onboarding templates), recipe (reserved for declarative YAML fixes).

**Recipe**
A declarative YAML fix definition — text replacement, regex substitution, or command execution. Simpler than a Pattern; authored, not learned.
_Avoid_: rule (reserved for detection rules), fix template.

**Campaign**
A multi-phase batch of related fixes. Groups Findings by rule or path, orders by dependency, and executes phases sequentially.
_Avoid_: batch (reserved for the batch PR engine), plan (too generic).

**Emergent Rule**
A new detection rule proposed from repeated Finding patterns. Lifecycle: PROPOSED → CANDIDATE → TENTATIVE → ACTIVE → RETIRED/REJECTED.
_Avoid_: custom rule, dynamic rule.

**Directive Seeding**
The feedback loop that injects prior fix attempts, lessons, failure patterns, and authoritative guidelines into LLM prompts so the engine avoids repeating failed fixes and follows industry-standard practice.
_Avoid_: context injection (too generic), prompt stuffing, lesson loop.

**Authoritative Guideline**
An industry-standard rule or convention that no linter enforces but the community treats as canon (PEP 8 prose, OWASP practices, style-guide conventions, framework idioms). Shipped as prompt-context files (not governed assets); loaded into LLM fix prompts by rule family via Directive Seeding. Distinct from a Recipe (which is a deterministic fix) and a Pattern (which is a learned fix).
_Avoid_: directive (reserved for the Directive Seeding mechanism), style rule (too generic), convention (reserved for Repo Taste Profile's repo-local conventions).

**Seed Library**
The collection of authoritative assets shipped with bluei itself: Golden Validation Bundles, seeded Patterns, built-in Recipes, and Authoritative Guidelines. Populated by the build-time ingestion pipeline from linter rules and industry guidelines. Ships ACTIVE (pre-vetted, bypasses governance). Distinct from mined assets (extracted from bluei's own work on consumer repos, governed through the Operator Control Plane). Plugin-extractable as a unit for future modular distribution.
_Avoid:_ built-in library (too generic), default patterns (too narrow — it includes bundles, recipes, and guidelines too).

**Reforge**
The routing step that classifies Findings as refactor candidates and sends them to the refactor queue for human-gated processing.
_Avoid_: refactor engine (too broad — reforge is specifically the classification + routing step).

**Worktree**
An isolated git worktree used for applying fixes without touching the main working directory. Used during fix application, validation, and review.
_Avoid_: branch (a worktree is backed by a branch but is a filesystem-level concept), sandbox.

**Validation Gate**
The post-fix check suite that confirms a fix doesn't introduce regressions — lint, type-check, test, and smoke-test pass.
_Avoid_: CI (external concept), verification (too close to verify_fix_closed).

**Deterministic Flywheel**
The product loop where successful novel fixes are converted into deterministic future capability: Patterns, Recipes, transforms, Golden Validation Bundles, or Emergent Rules, so recurring Findings need less LLM judgment over time.
_Avoid_: AI memory loop, optimization cycle, learning loop.

**Flywheel Ledger**
The observability surface that makes the Deterministic Flywheel measurable: how many Findings were resolved deterministically vs. by an LLM, which cascade stage won, Pattern replay outcomes, and the token/dollar cost avoided. Pure observation — it changes no fix behavior.
_Avoid_: metrics dashboard (too generic), telemetry dump, analytics.

**Pattern Court**
The evidence and decision layer for learned Patterns: why a Pattern matched, where it should not apply, and how it performed. Splits across releases — Court Lite (alpha.1) observes and explains; full Court (alpha.2) adds shadow learning and demotion/promotion.
_Avoid_: pattern validator (too narrow), pattern manager.

**Evidence Envelope**
The inspectable metadata surrounding a Pattern that makes it contestable: replay outcomes, last success/last failure, positive and negative path scope, and source Findings. Grows additively; never breaks Patterns written by older versions.
_Avoid_: pattern metadata (too generic), pattern record.

**Replay Outcome**
The three-way result of replaying a Pattern against a Finding: HIT (matched and passed the Validation Gate), MISS (candidate but not applicable — snippet or file absent), or FAILURE (matched but the Validation Gate rejected the fix). FAILURE is negative evidence; MISS is benign.
_Avoid_: replay result (ambiguous two-way), replay status.

**Repo Taste Profile**
A repo-local summary of conventions bluei has observed and can use when fixing Findings: preferred libraries, naming style, test style, framework idioms, and recurring exceptions.
_Avoid_: coding style (too narrow), team preferences (too broad), memory.

**Dry Replay**
The non-mutating evidence-collection path for learned Patterns: apply a Pattern's fix to a scratch copy, run the Validation Gate, record the would-have-applied outcome (HIT / MISS / FAILURE), discard the scratch. Runs alongside the cascade for matched-but-not-selected Patterns. Feeds promotion and demotion evidence.
_Avoid_: shadow replay (collides with review's `LiveRolloutMode.SHADOW`, which would-have-published a PR comment — different subsystem), dry run (too generic — `bluei duster` is a dry-run scan), shadow learning.

**Golden Validation Bundle**
A small regression fixture extracted from a successful fix, used to validate that a Pattern, Recipe, transform, or Emergent Rule still behaves correctly.
_Avoid_: golden test (ambiguous), fixture only, regression test.

**Governed Asset**
Any automation artifact that can auto-act on a Finding without per-action human approval, and therefore sits under the Operator Control Plane. Four subtypes in alpha.2: Pattern, Recipe, AST Transform, Emergent Rule. The unifying property is governance, not how the asset was created (Recipes are authored; Patterns are learned; both are Governed Assets).
_Avoid_: learned asset (misleading — Recipes are authored, not learned), learned automation, governed object.

**Approval Record**
The Operator Control Plane artifact that captures a single governance decision over a Governed Asset: actor, timestamp, decision (approve / reject / pause / demote / retire), reason, and evidence snapshot at decision time. Persisted once per decision in a single audit trail keyed by `AssetRef`.
_Avoid_: approval ticket, governance log, decision event.

**Governance State**
The Operator Control Plane's safety verdict on a Governed Asset, independent of the asset's native lifecycle. Triplet: `ACTIVE` (asset may auto-act), `PAUSED` (operator-suspended, temporarily skipped by cascade stages), `RETIRED` (operator-removed, permanently skipped). "An asset is effective" means `native_eligible AND Governance State == ACTIVE`.
_Avoid_: asset status (collides with Emergent Rule's native `status` field), promotion state, lifecycle state (reserved for the asset's own native machine).

**Asset Reference (AssetRef)**
The uniform handle for a Governed Asset across the control plane: `(asset_class, asset_id)`. Lets the approval inbox, audit trail, and Golden Reef bundle back-reference be asset-generic. `asset_class` is one of {pattern, recipe, transform, emergent_rule}.
_Avoid_: asset id (ambiguous — every asset has its own id shape), asset pointer.

**Escalation**
A structured event recorded when a cycle fails, merge fails, dedup saturation is detected, or rebase conflict trends increase. Written to `escalation_log.jsonl`.
_Avoid_: alert (reserved for notifications), incident.

## Resolved ambiguities

Previously overloaded terms, now disambiguated by architectural decision:

- **"State"** — RESOLVED (Rec 07). `StateManager` (`app/state.py`) delegates all shared schemas to canonical engine owners (`engine.state`, `engine.issue_lifecycle`, `engine.jsonl`). Engine owns schema and atomicity; StateManager is a path resolver. The `bluei/common/` layer provides cross-layer shared types. When discussing state, "StateManager" = the app-layer path resolver; "engine state functions" = the schema/IO owners.
- **"Escalation"** — RESOLVED (rec-09). `app/escalation.py` is a 10-line re-export shim; `engine/escalation.py` is the sole implementation (pattern detection + structured logging + status checking).
- **review → app coupling (H5)** — RESOLVED. `bluei/review/` is an independent subsystem: it imports from `bluei/engine/` and `bluei/common/` but never reaches into `bluei/app/`. Shared types (`Repo`, `ReviewMode`, `ReviewRun`, `FeedbackEvent`, etc.) live in `bluei/common/`.

## Example dialogue

> **Dev:** "The pr-cycle found 12 Findings. Three were classified as refactor-class by Reforge, so they went to the refactor queue. The remaining 9 went through the deterministic cascade — 4 matched Recipes, 2 matched Patterns, 3 fell through to Directive Seeding and got LLM fixes."
>
> **Domain expert:** "Good. Did the Validation Gate pass for all 6 deterministic fixes? And did the Campaign for the rust-rule batch finish its second phase?"
>
> **Dev:** "Yes — all passed. The Campaign is paused at phase 2 because of an Escalation: dedup saturation on the `clippy::needless_pass` rule. The Care Level for that repo is offer-fixes, so it won't auto-merge anyway."
>
> **Domain expert:** "Right. Lift the suppression on that rule and re-run the pr-cycle. The Emergent Rule for that pattern is in TENTATIVE — check if the shadow scan has enough evidence to promote it."
