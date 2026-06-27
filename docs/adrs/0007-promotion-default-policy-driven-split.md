# ADR-0007: Promotion default is policy-driven, split by write-producing vs in-place mutation

**Status:** accepted

When a Governed Asset meets its promotion trigger, the Operator Control Plane's default behavior is **policy-driven per asset class per repo**, with the shipped default policy splitting on blast radius:

- **Write-producing promotions are gate-closed by default.** The one silent auto-promotion path today is `promote_pattern_to_recipe` (`engine/shared_pattern_library.py:339`), which writes YAML into `recipes/staged/`. Promotion is queued in the approval inbox; nothing writes without `bluei learn approve <id>`.
- **In-place mutations are gate-open-with-audit by default.** Pattern confidence-threshold crossings (auto-replay eligibility) and Emergent Rule state transitions (TENTATIVE→ACTIVE) promote automatically per existing triggers; the operator can pause/demote/retire post-hoc, with every transition recorded in the audit trail.

## Why split, not uniform

Code reality: the two promotion flavors carry different blast radius.

- **Write-producing** produces a new persistent artifact (Recipe YAML) that propagates across runs and across repos via the shared library, and is hard to recall once other Recipes depend on it. Gate-closed is the only safe default.
- **In-place mutation** changes a reversible property of an existing asset (confidence score, lifecycle state) and is already protected by the Validation Gate at fix time. Gate-open-with-audit is acceptable; adding gate-closed here would regress Emergent Rule behavior — `bluei emergent promote` is already operator-invoked and shadow-gated today, so requiring a second approval would add friction without adding safety.

## Considered Options

- **(A) Gate-closed for everything.** Rejected — regresses Emergent Rule UX (already operator-invoked) without closing any new footgun; makes autonomous deployment feel broken.
- **(B) Gate-open with audit for everything.** Rejected — does not close the actual footgun (`promote_pattern_to_recipe` keeps silently writing YAML); the "Safe Learning" release name becomes a lie.
- **(C) Policy-driven with split default.** ✅ chosen. Closes the real footgun while preserving autonomous operation where it's already safe. Trusted repos can flip write-producing promotion to gate-open via config.

## Consequences

- A policy schema is part of alpha.2 scope, not deferred. Initial shape: per-asset-class, per-transition `mode` field (`gate_closed` | `gate_open_audit`).
- The single existing silent auto-promoter (`promote_pattern_to_recipe`) must be re-routed through the approval gate as part of alpha.2's behavior-changing slice. Until that wiring lands, the gate-closed default is fiction.
- Emergent Rule's existing `promote` verb remains operator-invoked; it gains audit-trail recording but does not require a second approval step.
- Pattern confidence-threshold promotion (e.g., crossing AUTO_REPLAY=0.9) remains automatic, but each crossing appends an Approval Record with decision=`auto_promote` and the evidence snapshot (success/failure counts, bundle refs) at decision time. Operator can demote post-hoc.
- Trusted repos can opt into gate-open write-producing promotion via policy; the default stays conservative.
