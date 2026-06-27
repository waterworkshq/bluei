# ADR-0006: Operator Control Plane governs all four acting asset classes from day one

**Status:** accepted

The Operator Control Plane (alpha.2 "Safe Learning") governs **Patterns, Recipes, AST transforms, and Emergent Rules** through a single unified substrate — generic `ApprovalRecord`, generic `AssetRef`, single audit trail, single approval inbox. It is *not* Patterns-first with other assets added later.

## Why all four from day one

Code reality: three of the four asset classes already auto-act on Findings without a per-action operator gate, and the fourth already has a full lifecycle with its own promotion verb:

- **Recipes** are a live cascade stage (`RecipeCascadeStage`, `engine/cascade.py:405`) with 10 builtin YAML catalogs, resolving Findings every clean run.
- **AST transforms** are a live cascade stage (`ASTTransformStage`, `engine/cascade.py:567`) with per-language pattern+transform modules.
- **Emergent Rules** have a full lifecycle (PROPOSED→CANDIDATE→TENTATIVE→ACTIVE→RETIRED), 7 CLI verbs, and feed discovery via `discover_active_rule_findings` + the review cycle's `emergent_rule_observer`.
- **Patterns** are the substrate alpha.1 instrumented (ReplayOutcome, Evidence Envelope, Court Lite).

More importantly, **`promote_pattern_to_recipe` (`engine/shared_pattern_library.py:339`) already auto-promotes high-confidence cross-repo Patterns into Recipes by writing YAML into `recipes/staged/` with no operator gate.** A Patterns-only control plane would govern the *least dangerous* asset (Patterns replay locally and roll back via the Validation Gate) while leaving the *more dangerous* silent-YAML-write path untouched. That inverts the safety priority the release exists to defend.

Building the substrate asset-generic from day one also avoids designing the approval schema twice and risking a migration when the second asset class is wired in.

## Considered Options

- **(A) Patterns only.** Rejected — leaves `promote_pattern_to_recipe` and Emergent Rule promotion ungoverned; designs the schema twice.
- **(B) Patterns + Emergent Rules.** Rejected — still leaves the Pattern→Recipe auto-promoter untouched.
- **(C) Patterns + Recipes + transforms.** Rejected — Emergent Rules already have richer lifecycle machinery than Patterns; excluding them fragments governance.
- **(D) All four through a unified substrate.** ✅ chosen.

## Consequences

- Substrate must be asset-generic: `AssetRef` carries `(asset_class, asset_id)`; `ApprovalRecord` references an `AssetRef`; audit trail and inbox are single stores keyed by `AssetRef`, not per-class.
- Ship order inside alpha.2 is **vertical by pillar, not horizontal by asset**: (1) generic control-plane schema + storage, (2) generic Golden Reef bundle container that any asset class can reference, (3) Dry Replay (Pattern-only mechanically today, generic record shape so AST transforms plug in during alpha.3), (4) re-route the existing `promote_pattern_to_recipe` and Emergent Rule `promote` verbs through the new approval gate.
- Repo Taste Profile entries and Model routing policies (also listed in seed 10) remain out of scope — they belong to alpha.4 "Repo Native" and alpha.5 "Economics" respectively. The substrate must be forward-compatible with them but they are not wired in alpha.2.
- Golden Validation Bundles are governed *by* the control plane but are not themselves Governed Assets — they are evidence artifacts referenced by assets. A bundle does not auto-act; it validates.
