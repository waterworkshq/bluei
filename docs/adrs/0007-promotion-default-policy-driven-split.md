# ADR-0007: Promotion default is policy-driven, split by write-producing vs in-place mutation

**Status:** refined 2026-06-29 (alpha.4 Phase 3 audit) — original split (write-producing vs in-place) holds; a second axis (runtime vs build-time) is added to the write-producing side. See "Refinement note" below.

When a Governed Asset meets its promotion trigger, the Operator Control Plane's default behavior is **policy-driven per asset class per repo**, with the shipped default policy splitting on blast radius:

- **Write-producing promotions are gate-closed by default *at runtime*.** The one silent auto-promotion path today is `promote_pattern_to_recipe` (`engine/shared_pattern_library.py:339`), which writes YAML into `recipes/staged/` during a fix cycle. Promotion is queued in the approval inbox; nothing writes without `bluei learn approve <id>`.
- **Write-producing promotions are gate-open-with-audit by default *at build time*.** Build-time pipelines (e.g. the Recipe Foundry, ADR-0020) that produce human-curated committed fixtures default to auto-write + `auto_promote` record. The human review + git commit before shipping IS the approval gate — stronger than any inbox loop. The runtime blast-radius argument (silent propagation across runs/repos) does not apply to artifacts that ship inert until the next release.
- **In-place mutations are gate-open-with-audit by default.** Pattern confidence-threshold crossings (auto-replay eligibility) and Emergent Rule state transitions (TENTATIVE→ACTIVE) promote automatically per existing triggers; the operator can pause/demote/retire post-hoc, with every transition recorded in the audit trail.

## Refinement note (2026-06-29, alpha.4 Phase 3 audit)

ADR-0020 (Recipe Foundry) defaulted the Foundry to `gate_open_audit`, which appeared to contradict this ADR's "write-producing = gate-closed" rule. Phase 3 audit surfaced that the contradiction stems from conflating two promoter categories:

1. **Runtime silent promoters** (`promote_pattern_to_recipe` during a fix cycle) — the original target of this ADR. Blast radius: writes YAML that propagates across runs/repos via the shared library, hard to recall. Gate-closed default is correct.
2. **Build-time curated pipelines** (Recipe Foundry, alpha.3 synthesize-then-validate seed pipeline) — produce committed product fixtures reviewed by a human before ship. Blast radius: none at runtime (fixtures are inert until the next release). The human commit IS the approval. Gate-open-with-audit default is correct.

The original ADR's rationale ("propagates across runs and across repos via the shared library, and is hard to recall once other Recipes depend on it") describes category 1 specifically. Category 2 didn't exist when this ADR was written (alpha.2 grilling). The refinement adds the runtime/build-time axis to the write-producing side; the in-place-mutation side is unaffected. The `resolve_policy(config, asset_class, transition)` API is unchanged — the default simply resolves differently based on whether the caller is runtime or build-time. Callers indicate their category by which code path they take (runtime cycle vs build-time pipeline), not by a new config field.

## Why split, not uniform

Code reality: the two promotion flavors carry different blast radius.

- **Runtime write-producing** produces a new persistent artifact (Recipe YAML) that propagates across runs and across repos via the shared library, and is hard to recall once other Recipes depend on it. Gate-closed is the only safe default.
- **Build-time write-producing** produces committed product fixtures that ship inert until the next release. Human review + commit is the approval. Gate-open-with-audit is safe; the audit trail records what the pipeline produced.
- **In-place mutation** changes a reversible property of an existing asset (confidence score, lifecycle state) and is already protected by the Validation Gate at fix time. Gate-open-with-audit is acceptable; adding gate-closed here would regress Emergent Rule behavior — `bluei emergent promote` is already operator-invoked and shadow-gated today, so requiring a second approval would add friction without adding safety.

## Considered Options

- **(A) Gate-closed for everything.** Rejected — regresses Emergent Rule UX (already operator-invoked) without closing any new footgun; makes autonomous deployment feel broken.
- **(B) Gate-open with audit for everything.** Rejected — does not close the actual runtime footgun (`promote_pattern_to_recipe` keeps silently writing YAML during cycles); the "Safe Learning" release name becomes a lie.
- **(C) Policy-driven with split default (runtime/build-time for write-producing).** ✅ chosen. Closes the real runtime footgun while preserving autonomous operation where it's already safe (in-place mutations, build-time curated pipelines). Trusted repos can flip any transition via config.

## Consequences

- A policy schema is part of alpha.2 scope, not deferred. Initial shape: per-asset-class, per-transition `mode` field (`gate_closed` | `gate_open_audit`).
- The single existing runtime silent auto-promoter (`promote_pattern_to_recipe`) must be re-routed through the approval gate as part of alpha.2's behavior-changing slice. Until that wiring lands, the gate-closed default is fiction.
- Build-time pipelines (Foundry, ADR-0020) default to gate-open-with-audit; their output is human-curated before commit. The `auto_promote` audit record documents what the pipeline produced; the commit history documents what a human approved for ship.
- Emergent Rule's existing `promote` verb remains operator-invoked; it gains audit-trail recording but does not require a second approval step.
- Pattern confidence-threshold promotion (e.g., crossing AUTO_REPLAY=0.9) remains automatic, but each crossing appends an Approval Record with decision=`auto_promote` and the evidence snapshot (success/failure counts, bundle refs) at decision time. Operator can demote post-hoc.
- Trusted repos can opt into gate-open runtime write-producing promotion via policy; the default stays conservative for runtime.
