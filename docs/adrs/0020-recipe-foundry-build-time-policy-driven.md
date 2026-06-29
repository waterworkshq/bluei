# ADR-0020: Recipe Foundry is a build-time LLM-author pipeline; governance approval is policy-driven

**Status:** accepted

## Context

alpha.4 introduces the Recipe Foundry, the first mechanism that turns the populated alpha.3 substrate into active determinism by producing deterministic Recipes from seeded Patterns. Three intertwined design questions needed resolution:

1. **LLM output format.** bluei has no in-process LLM SDK — all LLM work is shelled out via `apply_claude_fix(ClaudeFixRequest)` for fix *application* (writes a fix into a worktree). The Foundry needs LLM *authoring* (returns YAML). The Recipe schema is rich (`RecipeMatch` with type/pattern/rule/prefix/scope/context_guard; `RecipeReplacement` with type/value/command/template_file/pattern/replacement/prepend_*/condition/count; `RecipeValidation`; `metadata` as `Dict[str, Any]`).

2. **Approval loop.** The existing `promote_pattern_to_recipe` (shared_pattern_library.py:340) is the only writer of `pending_approval` records, and only under explicit gate-closed policy for cross-repo patterns with a bundle reference. It has a "front door" (propose → pending) but no "back door" — there is no CLI to approve, and nothing re-runs the YAML write after approval. The Foundry needs the full loop OR an auto-approval path.

3. **CLI surface.** ADR-0015 split the Operator Control Plane surface into "unified read under `learn`, native writes per asset class." The Foundry introduces two distinct CLI needs: a trigger to propose Recipes (asset-content write), and governance decision verbs (approve/reject/pause — control-plane writes that don't exist as CLI commands anywhere today, since `emergent approve` flips native lifecycle, not Governance State).

A Phase-0-grade finding during grilling also established that `seeded_patterns/` is empty (alpha.3 shipped Bundles but not Patterns); alpha.4 Slice 0 regenerates them via the existing packaging pipeline. The Foundry's input corpus is therefore the Slice-0-populated seeded Patterns.

## Decision

### 1. Foundry is a build-time tool with no runtime CLI trigger

The Foundry lives in `bluei/tools/foundry/` (parallel to `bluei/tools/seed/pipeline.py`) and runs as a build script (`python -m bluei.tools.foundry` or equivalent). It has NO `bluei` CLI trigger. This mirrors the alpha.3 synthesize-then-validate pipeline precedent (ADR-0018): build-time pipelines that produce committed product fixtures are not runtime operator commands.

Runtime never invokes the Foundry. Its output (staged Recipe YAML) is what `RecipeEngine` loads at runtime.

### 2. LLM output format = full Recipe YAML, validated and stripped (γ)

The Foundry's LLM invocation uses a new shell-out command template (`claude_author_cmd_template`, parallel to the existing `claude_cmd_template` for fix application). The prompt contains: the Recipe schema summary, 2–3 gold examples (one `text`, one `regex_substitute`, one `command`), and the seeded Pattern's `before`/`after`/`rule`/`language`/`imports_touched`. The LLM emits a complete Recipe YAML in a fenced block.

The Foundry then:
- Extracts the fenced YAML block.
- Parses with `load_recipe` (strict schema); rejects on parse error.
- **Strips unknown keys** (defensive against hallucinated fields; `metadata` survives because it's `Dict[str, Any]` but unknown top-level keys are dropped).
- Runs the rule's detector on `before` (must trigger) and `after` (must come back clean) as oracle — the same validation the synthesize-then-validate pipeline uses (ADR-0018 pattern reuse).
- Rejects on any mismatch; logs the rejection reason.

Hallucination risk is contained by three factors: build-time only, strict schema validation, and human curation before commit (the first Foundry run's output is quality-reviewed per the alpha.3 post-release lesson before becoming the shipped canonical seeded-Recipe set).

### 3. Approval loop is policy-driven; auto-approval is the default

The Foundry respects the existing `resolve_policy(config, "recipe", "write_producing")` (ADR-0007), exactly as `promote_pattern_to_recipe` does for the `pattern` class:

- **`gate_open_audit` (build-time default per ADR-0007's refined runtime/build-time axis)** — Foundry writes the Recipe YAML to `recipes/staged/` and appends an `auto_promote` ApprovalRecord. **Auto-approval: no inbox entry.** This is the appropriate default for build-time proposals from pre-vetted seeded-Pattern input; the human review + git commit before shipping IS the approval gate (stronger than any inbox loop). The runtime footgun ADR-0007 originally targeted (silent propagation across runs/repos) does not apply to committed fixtures that ship inert until the next release.
- **`gate_closed` (explicit policy config, for operators who want inbox oversight of the build pipeline)** — Foundry does NOT write YAML. It caches the proposed Recipe YAML inline in the ApprovalRecord's `evidence_snapshot` field (already JSON-serializable, already part of the schema) and appends a `pending_approval` record. The proposal surfaces in `bluei learn inbox`. Operator approval triggers `resume_pending_promotion`, which reads the cached payload from `evidence_snapshot` and writes the YAML to `recipes/staged/`.

Both paths ship; the policy file chooses. The `gate_closed` path delivers the seed-14 "first non-empty inbox" headline story for operators who want oversight; the default `gate_open_audit` path delivers auto-approval for build-time curated pipelines.

### 4. Asset class = `recipe` (not `pattern`)

Foundry-written ApprovalRecords use `asset_class="recipe"` per ADR-0006 (Recipe is a distinct governed class). The existing `promote_pattern_to_recipe` path uses `asset_class="pattern"` — a pre-existing mismatch noted but not fixed in alpha.4 (separable).

### 5. Governance decision verbs live under `bluei learn` (revises ADR-0015)

The Foundry's `gate_closed` path requires governance decision verbs (approve/reject/pause/resume/retire) that write ApprovalRecords. These don't exist as CLI commands today. They are added under `bluei learn` (`bluei learn approve <asset_ref>`, etc.) per ADR-0015's revision (see that ADR's updated rationale). The inbox is already under `learn`; colocating the decision verbs closes the UX loop.

## Rationale

- **Build-time trigger (no CLI) is consistent with C3 and ADR-0018.** The Foundry consumes seeded Patterns (build-time corpus), produces committed fixtures, and is invoked by a build step — not by an operator during a fix cycle. Inventing a runtime CLI namespace (θ/ι) for a build-time tool would misrepresent its locus.
- **Full-YAML-with-validation (γ) over constrained intermediate (β).** The existing Recipe variety (`text`/`regex_substitute`/`command`/`scaffold` handlers) cannot be expressed by a 5-field intermediate. Strict schema validation + unknown-key stripping + detector oracle + human curation together contain the hallucination risk that free-form α would carry.
- **Policy-driven approval honors both the auto-approval default AND the inbox story.** Mandatory manual approval (pure δ) was rejected by the user as unsuitable for a build-time curated pipeline. Auto-only (pure ε) would void the seed-14 governance-consumer headline. The existing ADR-0007 policy dial resolves both cleanly: default auto, opt-in manual.
- **Caching the proposed YAML in `evidence_snapshot` avoids a new state file.** The ApprovalRecord schema already has this field; using it for the pending payload keeps the audit trail as the single source of truth (ADR-0008) and makes the proposal self-describing — `bluei learn audit <ref>` shows not just that something was proposed but exactly what.
- **`recipe` asset class aligns with ADR-0006's four-class taxonomy.** Conflating Recipes under `pattern:` would lose the governance distinction between authored and learned assets.

## Considered alternatives

- **LLM format:** (α) full free-form YAML — rejected, hallucination risk even with curation; (β) constrained intermediate JSON — rejected, can't express command/scaffold handlers; (γ, chosen) full YAML + strict validation + strip + oracle.
- **Approval loop:** (δ) mandatory manual approve loop — rejected by user (unsuitable for build-time); (ε) auto-only gate-open — rejected, voids seed-14 story; (policy-driven δ machinery + ε default, chosen) respects both.
- **CLI trigger:** (η, chosen) no CLI, build script; (θ) `bluei recipes propose` — rejected, invents runtime namespace for build-time tool; (ι) `bluei patterns propose-recipe` — rejected for the same reason and misrepresents the output class.
- **Governance verbs locus:** (κ, chosen) under `learn` next to inbox; (λ) new `bluei govern` namespace — rejected, 3 verbs don't justify a namespace; (μ) per-asset native — rejected, re-fragments what ADR-0015 unified.

## Consequences

- **New build-time package:** `bluei/tools/foundry/` (outside `enforce_architecture.py` scope, parallel to `bluei/tools/seed/`). Contains the LLM prompt assembly, shell-out invocation, YAML extraction/validation, oracle validation, and `package_recipe` writer to `recipes/staged/`.
- **New config key:** `claude_author_cmd_template` (parallel to `claude_cmd_template`). Foundry is gated behind its existence — no template → Foundry disabled, not crashed.
- **New runtime library:** `resume_pending_promotion(asset_ref, approval_records_path, output_dir)` — reads cached YAML from `evidence_snapshot`, writes to `recipes/staged/`. Called by `bluei learn approve` for `recipe:` asset_refs with pending history.
- **CLI additions under `bluei learn`:** `approve`, `reject`, `pause`, `resume`, `retire` subcommands taking `<asset_ref>`. These write ApprovalRecords only (no asset-content mutation); they are cross-asset governance decisions. Revises ADR-0015.
- **`recipes/staged/` becomes load-bearing for seeded content.** `RecipeEngine` already loads it; the canonical seeded-Recipe set (from the first curated Foundry run) ships here. A future `recipes/seeded/` split is not needed — the staged/built-in distinction (built-in = shipped canonical; staged = operator/Foundry-proposed) already covers the curation boundary.
- **`promote_pattern_to_recipe` is NOT removed or rewritten.** It serves a different path (cross-repo Pattern → Recipe, gate-routed). The Foundry serves (seeded Pattern → Recipe, build-time). Both coexist; both write ApprovalRecords under their respective asset_class policies.
- **First non-empty `bluei learn inbox` requires gate_closed config.** Under default `gate_open_audit`, the inbox stays empty (auto-promoted). Operators who want the seed-14 inbox UX configure `learning.asset_classes.recipe.write_producing: gate_closed`.
- **Inert in alpha.4 runtime.** Per the inherited constraint (no live runs before stable), the Foundry runs at build time only; its runtime footprint is the committed Recipe YAMLs loaded by `RecipeEngine`. The approve loop is exercised in synthetic tests only.
