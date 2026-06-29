# ADR-0015: `bluei learn` is the unified read/inbox + governance-decision surface; asset-content writes stay native

**Status:** revised 2026-06-29 (alpha.4 grilling) — original decision (unified read under `learn`) holds; write-side framing narrowed to distinguish three write categories. See "Revision note" below.

The Operator Control Plane's operator-facing CLI surface is **split by intent**:

- **Unified read path under `bluei learn`** — asset-generic commands that answer cross-asset-class questions:
  - `bluei learn inbox` — list pending approvals across all Governed Asset classes (the operator's worklist).
  - `bluei learn status <asset_ref>` — render native state + Governance State + recent Dry Replay / SPRT evidence for any asset.
  - `bluei learn audit <asset_ref>` — full ApprovalRecord history for an asset.
- **Unified governance-decision path under `bluei learn`** — cross-asset control-plane writes that flip Governance State (ADR-0008) by appending ApprovalRecords:
  - `bluei learn approve <asset_ref>` / `reject` / `pause` / `resume` / `retire` — the decision verbs that consume the inbox. These write ApprovalRecords only; they do not mutate asset content.
- **Native asset-content write path** — verbs that edit the asset itself stay in their existing namespaces:
  - `patterns exclude` / `patterns unexclude` (Pattern asset-content: sets `excluded_paths`)
  - `emergent approve` / `emergent promote` / `emergent reject` / `emergent retire` (Emergent asset-content: flips native `EmergentRuleStatus`, NOT Governance State)
  - Recipe Foundry proposals (build-time, no CLI — see ADR-0020)

## Revision note (2026-06-29, alpha.4)

The original ADR framed the split as "read under `learn`, writes native." Grilling for alpha.4 surfaced that "writes" conflated three distinct categories:

1. **Governance State decisions** (approve/reject/pause that write ApprovalRecords and flip the ADR-0008 Governance State overlay) — inherently cross-asset, exactly like reads. These move UNDER `learn` next to the inbox.
2. **Native lifecycle transitions** (`emergent approve` flips `EmergentRuleStatus.PROPOSED→ACTIVE`) — asset-class-specific, correctly stay native.
3. **Asset-content edits** (`patterns exclude` sets `excluded_paths`; Foundry writes Recipe YAML) — asset-class-specific, correctly stay native.

The original ADR's "native writes" examples included `patterns pause` and `emergent approve` as if both were the same category. They are not: `patterns pause` (had it shipped — it was aspirational, never implemented) would be a governance decision (category 1); `emergent approve` is a native lifecycle transition (category 2). The revision makes the categorisation explicit and routes category 1 under `learn`.

No shipped verb migrates. The only shipped native write-verbs today are `patterns exclude`/`unexclude` (category 3) and `emergent {approve,reject,retire,promote,gc}` (category 2). Category 1 verbs are NEW in alpha.4 (required by the Recipe Foundry's gate-closed path, ADR-0020) and land under `learn` from the start.

## Why split, not unified or fully native

- **(A) Unified `bluei learn` for everything, deprecate existing verbs.** Rejected — `patterns exclude`/`emergent promote` are documented in the README and operator guide. Migrating them breaks existing operators' scripts and habits for the benefit of future operators who don't exist yet.
- **(B) Fully native (`patterns approve`, `emergent approve`, …).** Rejected — fragments the substrate's unified promise (ADR-0006). An operator with a pending Pattern→Recipe promotion and a pending Emergent Rule promotion would run two different commands for the same decision type.
- **(C, revised) `bluei learn` unifies read + cross-asset governance decisions; native verbs keep asset-content + native-lifecycle writes.** ✅ chosen. Matches how operators use governance: the cross-asset questions ("what's pending?", "approve this") are the point of the substrate, and those are unified; the per-asset content edits and native lifecycle transitions already work and are documented.

## Inbox rendering priority

**CLI primary, dashboard secondary.** Operators on cron-driven deployments interact via CLI; the inbox is their worklist and ships first. The dashboard card renders the same inbox data read-only, shipping once the CLI shape is stable. The Flywheel Ledger card (alpha.1) already established the dashboard's "Learning" section; the inbox card is additive.

## Consequences

- **The `learn` namespace now hosts both reads and cross-asset governance decisions.** The inbox lists pending; `learn approve`/`reject`/`pause`/`resume`/`retire` decide them. Asset-content writes (`patterns exclude`, Foundry proposals) and native lifecycle transitions (`emergent approve`) stay in their native namespaces.
- **The AssetRef (`pattern:fp-...`, `recipe:auto-...`, `transform:python-b904-raise`, `emergent_rule:er-...`) is the uniform handle across reads, governance decisions, and audit.** `bluei learn approve recipe:auto-ruff-b904-a1b2c3d4` works the same as `bluei learn approve pattern:fp-a1b2c3d4`.
- **The audit trail (`learn audit`) is the single source of truth for governance history.** Native write verbs and `learn` decision verbs both append to it; the unified read verbs read from it. No per-asset audit stores.
- **Native lifecycle verbs (`emergent approve`) do NOT write ApprovalRecords.** They flip native state (e.g. `EmergentRuleStatus`). This is correct: native lifecycle and Governance State are independent overlays (CONTEXT.md). An emergent rule can be native-ACTIVE but governance-PAUSED; the cascade consults both.
- **Migration cost: zero existing verbs change.** `patterns exclude` continues to work (writes `excluded_paths`, an asset-content edit); operators who discover `learn inbox` + `learn approve` can use them, operators who don't aren't forced to.
