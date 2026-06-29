# ADR-0019: Structural Replay lives in the Pattern replay stage with a two-stage confidence gate

**Status:** accepted

## Context

alpha.3 shipped seeded Patterns at prompt-hint confidence (0.5–0.9) because their canonical `before_snippet` cannot literally appear in real code — `_find_snippet_in_file` (pattern_replay.py) does a literal text match and returns None for any rename, import difference, or layout variation. The TODO at pattern_replay.py:182 explicitly flags the gap: cross-repo and seeded Patterns cannot auto-apply until AST-based structural replay exists. alpha.4 ("Deterministic Assets") needs to close this gap to activate the populated substrate.

Two independent systems already touch AST-based fixes:

- **`ASTTransformCascadeStage`** (cascade.py:603) — applies hand-authored op-code transforms (`prepend_method`, `wrap_set`, `insert_guard`, `replace_op`, …) keyed by non-linter rules (`perf-pop-front-loop`, `notifications-email-no-trim`, …). Already governance-aware (`is_governance_active(format_asset_ref("transform", "python-<rule>"), gs)`). Eight transforms shipped in `python_transforms.py`.
- **`PatternReplayCascadeStage`** — resolves a Pattern via `lookup` → `lookup_structural` (AST hash) → `lookup_fuzzy` (Levenshtein), then applies via literal text splice. Structural *lookup* already works; structural *application* does not.

Pattern-derived edits (a before/after pair with variable bindings) have a different shape from hand-authored op-code transforms (a fixed transform_type with static params). They also produce different evidence: Pattern application records HIT/MISS/FAILURE, feeds SPRT demotion (ADR-0012) and Dry Replay (ADR-0011); hand-authored transforms record only success/failure into a different sink.

## Decision

**Structural Replay is a fallback *application* step inside `PatternReplayCascadeStage`, implemented in a new peer module `bluei/engine/structural_replay.py`.** It is NOT a new cascade stage, NOT a new transform_type in `ASTTransformCascadeStage`, and NOT a replacement for the literal path.

### Application flow (within `PatternReplayCascadeStage.attempt`)

1. Resolve pattern (existing logic — unchanged).
2. Try literal `_find_snippet_in_file`. On success → existing text-splice path (unchanged).
3. On literal miss → call `apply_structural_replay(file_text, pattern, language) -> Optional[str]`:
   - Parse `pattern.before_snippet` and the candidate target region into ASTs.
   - Structural match with a variable-binding map (rename-tolerant via the existing `_PythonStructuralNormalizer`).
   - Derive the AST edit from the Pattern's before→after delta; apply the same edit to the matched target subtree using the binding map (variable renaming, import additions).
   - `ast.unparse` + `compile()` check; return new source on success, `None` on structural miss or invalid output.
4. On success → write + baseline validation → record HIT/FAILURE evidence as today.
5. On structural miss → record MISS evidence as today (no behavior change for Patterns that neither literal- nor structural-match).

### Two-stage confidence gate

Structural application activates only when BOTH gates pass:

- **Eligibility:** Pattern confidence ≥ `AUTO_REPLAY_THRESHOLD` (0.9) — unchanged from the literal path.
- **Application:** `fuzzy_structural_match ≥ get_fuzzy_threshold(rule, catalog)` AND binding-map completeness = 1.0 (every variable/import binding in the Pattern resolved to a target-side counterpart).

The eligibility gate uses the existing threshold (no regression for literal-applied Patterns). The application gate is two independent checks: the fuzzy score captures "same shape"; binding-map completeness captures "no dangling references." A Pattern referencing a variable absent in the target is a semantic mismatch even when the shape is similar.

Low-confidence Patterns (`PROMPT_HINT_THRESHOLD ≤ confidence < AUTO_REPLAY_THRESHOLD`) continue to surface as prompt hints only — Structural Replay does not activate below auto-replay tier.

## Rationale

- **Evidence-path unity.** Structural Replay IS Pattern replay (same HIT/MISS/FAILURE outcomes, same SPRT/Dry Replay consumers). A separate stage would split evidence across two cascade positions; an `ASTTransformCascadeStage` integration would route Pattern evidence into the transform sink. Keeping application inside `PatternReplayCascadeStage` means one evidence stream feeds all consumers (ADR-0011 Dry Replay, ADR-0012 SPRT, ADR-0001 Flywheel Ledger).
- **`ASTTransformCascadeStage` stays purpose-clean.** It is for hand-authored transforms keyed by rule. Forcing Pattern-derived edits into its op-code model would create a `structural_replay` mega-op that internally does what the peer module does — better to make the peer module first-class and let the stage dispatch to it on literal miss.
- **Two-stage gate defends a failure mode literal replay doesn't have.** Literal application is exact by definition (snippet is present). Structural match is fuzzy (rename-tolerant), so "the Pattern is trustworthy" (confidence) and "this target is the same shape" (match score + binding completeness) are independent questions. Conflating them lets a 0.95 Pattern fire on a 0.3-quality match. The Validation Gate catches *broken* applies but not *semantically-wrong-but-valid* ones (e.g. renaming the wrong variable); the match score is the pre-mutation defense, the Validation Gate is the post-mutation defense.
- **Symmetric with the resolver.** `_resolve_pattern` already cascades lookup → lookup_structural → lookup_fuzzy. Extending *application* with a literal→structural fallback mirrors that pattern.

## Considered alternatives

- **(A) Extend `ASTTransform` with a `structural_replay` transform_type.** Rejected: op-codes were designed for hand-authored fixed transforms. A `structural_replay` op would internally reimplement the peer module's logic; the dispatch indirection adds nothing. Also splits Pattern evidence into the transform sink.
- **(B) New cascade stage `StructuralReplayCascadeStage`.** Rejected: duplicates the Pattern resolution + evidence plumbing of `PatternReplayCascadeStage`. Also forces an ordering decision (before or after literal replay?) that doesn't exist when the two share a stage.
- **(C) AST-diff/patch (gumtree-style).** Rejected for alpha.4: structural matching with renaming tolerance is the needed scope; arbitrary-edit AST diffing is a research-grade problem and overkill. May be revisited if Pattern before/after deltas grow complex post-stable.
- **(D, chosen) Fallback inside `PatternReplayCascadeStage` via peer module.** Unified evidence, clean layering, symmetric with resolver.

## Consequences

- **New module:** `bluei/engine/structural_replay.py` exporting `apply_structural_replay(file_text, pattern, language) -> Optional[str]`. Imported only by `pattern_replay.py`. No layering change (engine→engine).
- **`ASTTransformCascadeStage` unchanged.** Hand-authored transforms keep their path; no conflation with Pattern-derived edits.
- **No cascade order change.** `default_cascade_stages()` still returns linters → recipes → pattern replay → composite → AST.
- **`AUTO_REPLAY_THRESHOLD` unchanged.** Same eligibility gate for literal and structural application. No regression for existing literal-applied Patterns.
- **New runtime check: binding-map completeness.** Every variable/import binding in the Pattern must resolve to a target-side counterpart. Unresolved binding → structural miss → MISS evidence. This is stricter than the fuzzy match alone and is the primary defense against semantically-wrong applies.
- **Cross-repo confidence cap (0.49, pattern_replay.py:184) is NOT lifted by this ADR.** The cap exists because literal application fails for privacy-normalized snippets; Structural Replay removes that *technical* reason, but cross-repo mined Patterns do not exist before 0.2.0 stable and seeded Patterns already bypass the cap (alpha.3 grilling Q4 — canonical, not privacy-normalized). Lifting the cap for true cross-repo Patterns is a post-stable decision when real cross-repo evidence exists.
- **TypeScript Structural Replay deferred.** `structural_hash/` has only `python.py` + `text.py`; the binding-map extractor is Python-AST-based. TS support requires a tree-sitter binding extractor and lands in a later release (seed 06 risk note).
- **Inert in alpha.4 runtime.** Per the inherited constraint (no live runs before stable), Structural Replay ships proven by synthetic tests only. The first real application occurs post-stable; SPRT/Dry Replay evidence for structurally-applied Patterns accumulates from that point.
