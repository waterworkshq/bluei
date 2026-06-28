# ADR-0017: Authoritative Guidelines are prompt-context, not governed assets

**Status:** accepted

Authoritative Guidelines (PEP 8 prose, OWASP practices, style-guide conventions, framework idioms) are shipped as prompt-context files loaded into LLM fix prompts via Directive Seeding — NOT as a fifth governed asset class alongside Patterns, Recipes, AST Transforms, and Emergent Rules.

## Context

alpha.3 needs to seed extra-linter authoritative guidelines (industry-standard practice too difficult to articulate as deterministic fixes). The question was whether these become a new governed asset class (requiring ADR-0006 scope expansion, `bluei learn` namespace changes, governance gate integration) or use a lighter mechanism.

## Decision

Guidelines are prompt-context files. A new `authoritative_guidelines/` directory holds structured files; a runtime loader feeds them into `render_claude_fix_prompt` by rule family. They are NOT governed — no approval, pause, retire, or audit trail through the Operator Control Plane. Provenance is tracked by storage locus (product directory = `authoritative-seed`).

## Rationale

Governance (approval/pause/retire) adds complexity that is only valuable when operators need to control individual assets. Seeded authoritative guidelines are pre-vetted industry canon — there is no operator decision to make about whether "validate inputs at trust boundaries" should be ACTIVE. Governance becomes justified when alpha.5 Taste Atlas introduces repo-local taste entries that operators may want to approve, reject, or pin. At that point, repo-local taste can promote to governed assets; the authoritative seed library stays ungoverned.

The data transfers cleanly if promotion happens later — guideline files become governed asset records with no content migration.

## Consequences

- ADR-0006's four governed asset classes (Pattern, Recipe, AST Transform, Emergent Rule) remain the complete set. Guidelines are a distinct non-governed category.
- `bluei learn inbox`, `bluei learn status`, and `bluei learn audit` do not surface guidelines — they have no governance lifecycle.
- alpha.5 Taste Atlas may introduce governed repo-local taste entries as a fifth asset class if operator approval semantics are needed. That decision is deferred to alpha.5's grilling.
