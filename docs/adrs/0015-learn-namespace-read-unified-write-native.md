# ADR-0015: `bluei learn` is the unified read/inbox surface; write-verbs stay native

**Status:** accepted

The Operator Control Plane's operator-facing CLI surface is **split by intent**:

- **Unified read path under `bluei learn`** — asset-generic commands that answer cross-asset-class questions:
  - `bluei learn inbox` — list pending approvals across all Governed Asset classes (the operator's worklist).
  - `bluei learn status <asset_ref>` — render native state + Governance State + recent Dry Replay / SPRT evidence for any asset.
  - `bluei learn audit <asset_ref>` — full ApprovalRecord history for an asset.
- **Native write path** — governance verbs stay in their existing namespaces but route through the unified ApprovalRecord store:
  - `patterns pause` / `patterns resume` / `patterns retire` (Pattern-native)
  - `emergent approve` / `emergent promote` / `emergent pause` (Emergent-native)
  - Recipe and AST transform governance via... (no existing namespace — see Consequences)

## Why split, not unified or fully native

- **(A) Unified `bluei learn` for everything, deprecate existing verbs.** Rejected — `patterns exclude`/`emergent promote` are documented in the README and operator guide. Migrating them breaks existing operators' scripts and habits for the benefit of future operators who don't exist yet.
- **(B) Fully native (`patterns approve`, `emergent approve`, …).** Rejected — fragments the substrate's unified promise (ADR-0006). An operator with a pending Pattern→Recipe promotion and a pending Emergent Rule promotion would run two different commands for the same decision type.
- **(C) `bluei learn` read/inbox unified; native writes.** ✅ chosen. Matches how operators use governance: the cross-asset question ("what's pending across everything?") is the point of the substrate, and that's unified; the per-asset write verbs already work and are documented.

## Inbox rendering priority

**CLI primary, dashboard secondary.** Operators on cron-driven deployments interact via CLI; the inbox is their worklist and ships first. The dashboard card renders the same inbox data read-only, shipping once the CLI shape is stable. The Flywheel Ledger card (alpha.1) already established the dashboard's "Learning" section; the inbox card is additive.

## Consequences

- **Recipes and AST transforms have no existing CLI namespace.** They need write-verbs somewhere. Two sub-options, deferred to PRD: (i) `bluei learn pause recipe:<id>` falls back to the unified namespace when no native namespace exists, or (ii) a minimal `bluei recipe` / `bluei transform` namespace is introduced for governance only. Option (i) is pragmatic and keeps the surface small; the `learn` namespace *can* host writes for asset classes that don't have a native home, while deferring to native namespaces where they exist.
- The AssetRef (`pattern:fp-...`, `recipe:auto-...`, `transform:python-b904-raise`, `emergent_rule:er-...`) is the uniform handle across both the read and write surfaces. `bluei learn status recipe:auto-ruff-b904-a1b2c3d4` works the same as `bluei learn status pattern:fp-a1b2c3d4`.
- The audit trail (`learn audit`) is the single source of truth for governance history. Native write verbs append to it; the unified read verbs read from it. No per-asset audit stores.
- Migration cost: zero existing verbs change. `patterns exclude` continues to work (it writes an ApprovalRecord under the hood); operators who discover `learn inbox` can use it, operators who don't aren't forced to.
