# ADR-0008: Governed Asset state is a minimal overlay triplet, native lifecycle preserved

**Status:** accepted

The Operator Control Plane owns only a **Governance State** triplet — `ACTIVE` / `PAUSED` / `RETIRED` — overlaid on each asset's native lifecycle. It does not define a unified state machine that all four asset classes must conform to. "Promotion" is a check-point *event* the control plane intercepts at the cascade stage, not a state.

## Why overlay, not unified

Code reality: the four asset classes have genuinely different native lifecycles and storage shapes.

- **Pattern** — implicit lifecycle via `confidence` score + `excluded_paths` (Court Lite). No status field.
- **Recipe** — no state at all. A Recipe exists iff its YAML file exists in `recipes/built-in/` or `recipes/staged/`. `recipe_engine.match()` loads by glob.
- **AST transform** — code, not data. Ships as Python source (`PYTHON_TRANSFORMS`, `TS_TRANSFORMS`, …). Cannot have a runtime status field without a loader-level consult.
- **Emergent Rule** — richest native machine: `PROPOSED→CANDIDATE→TENTATIVE→ACTIVE→RETIRED/REJECTED` via `EmergentRuleStatus`.

A unified state machine would force Recipes and transforms to gain state they don't have, and would either collapse Emergent Rule's lifecycle (losing richness) or duplicate it (governance theater). The control plane's job is *safety*, not lifecycle management.

## How it works

- **Governance State is a read-time projection, not a stored field.** The ApprovalRecord trail (`repos/<name>/state/approval_records.jsonl`, append-only event log) IS the governance store. Governance State (ACTIVE / PAUSED / RETIRED) is derived at cycle start by scanning records — the most recent decision per AssetRef determines the current state. No separate `governance_state.json` file. This is event-sourcing: state = projection of the event log. Benefits: no state synchronization issues (the state IS the log), crash-safe (append-only, no in-place state updates), auditable (every state change has a full decision record).
- **Cascade-stage consultation layer.** At cycle start, project the ApprovalRecord trail into an in-memory dict mapping AssetRef → Governance State. Each of the four cascade stages (`RecipeCascadeStage.attempt`, `ASTTransformStage.attempt`, `PatternReplayCascadeStage.attempt`, `discover_active_rule_findings`) gains a single check against the in-memory dict: `is_governance_active(asset_ref)`. No per-attempt file reads.
- **Promotion as event, not state.** When a native transition would activate an asset (Pattern crosses `AUTO_REPLAY=0.9`, `promote_pattern_to_recipe` would write YAML, Emergent Rule `TENTATIVE→ACTIVE`), the control plane intercepts:
  - **Gate-closed policy** (write-producing, per ADR-0007): native transition is held; an Approval Record is appended to the trail; the transition completes only on `bluei learn approve`.
  - **Gate-open policy** (in-place mutation, per ADR-0007): native transition proceeds; an Approval Record with `decision=auto_promote` is appended with the evidence snapshot.
- **Approval Record carries native state.** Each record captures `(asset_ref, native_state_before, native_state_after, decision, reason, evidence_snapshot, actor, timestamp)`. The audit trail is *more* informative than a unified vocabulary because it preserves native detail.

## Operator actions mapped to the overlay

| Action | Effect on Governance State | Effect on native lifecycle |
|---|---|---|
| `approve` (pending → active) | `→ ACTIVE` | Held native transition completes |
| `reject` (pending → retired) | `→ RETIRED` | Held native transition is discarded |
| `pause` (active → paused) | `→ PAUSED` | Native state unchanged; cascade stage skips |
| `demote` (active → paused or active→active w/ native demotion) | `→ PAUSED` or unchanged | Native confidence lowered / status pushed back |
| `retire` (any → retired) | `→ RETIRED` | Native asset may remain on disk for audit; cascade stage skips permanently |
| `resume` (paused → active) | `→ ACTIVE` | Native state unchanged |

## Considered Options

- **(A) Unified state machine, all classes conform.** Rejected — forces state onto Recipes (none today) and AST transforms (code, not data); collapses or duplicates Emergent Rule's native lifecycle.
- **(B) Per-class native, governance record only.** Rejected — pause/retire have no native representation for Recipes/transforms; "what state is this asset in?" becomes unanswerable without consulting two stores.
- **(C) Governance overlay triplet on native lifecycle.** ✅ chosen. Control plane owns safety only; native lifecycle continues to drive auto-activation; consultation happens at four known cascade-stage chokepoints.

## Consequences

- `bluei learn status <asset_ref>` must render both native state and Governance State, because "active" means `native_eligible AND governance_state == ACTIVE`. This is the UX cost of the overlay design.
- Four cascade stages gain a single consult call. The consultation is cached per-cycle to avoid repeated lookups for the same asset.
- Native lifecycle transitions that activate an asset are now interceptable events. The interception point for each class:
  - Pattern: `FixPatternStore.lookup` family (already where `excluded_paths` is enforced).
  - Recipe: `promote_pattern_to_recipe` write path + `RecipeCascadeStage` match filter.
  - AST transform: `ASTTransformStage.attempt` before invoking the transform registry.
  - Emergent Rule: `EmergentRuleStore` status transitions + `discover_active_rule_findings` filter.
- Emergent Rule's existing `approve` / `promote` verbs remain; they gain audit-trail recording but do not require a second approval when their policy is gate-open.
