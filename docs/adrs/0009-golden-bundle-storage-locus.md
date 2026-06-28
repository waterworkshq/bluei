# ADR-0009: Golden Validation Bundles split across product fixtures and repo state, privacy-boundaried by filesystem

**Status:** accepted (amended 2026-06-27 — product fixtures reframed as self-seeded library)

Golden Validation Bundles live in **two filesystem roots** with a hard privacy boundary between them:

- **Product fixtures** at `bluei/engine/golden_bundles/` — ship with bluei, curated, public. **Primary population mechanism** (populated by the alpha.3 synthesize-then-validate pipeline per ADR-0018). Candidates for future cross-repo sharing.
- **Repo state** at `repos/<name>/state/golden_bundles/` — operator-local, never exported, as private as the `findings.jsonl` they were extracted from. Starts empty; grows only when operators create bundles via `bluei learn bundle` from known-good fixes.

## Why hybrid, and why filesystem-boundaried

Bundle validation requires the **original code** to run the detector. A privacy-normalized snippet cannot be validated against `ruff` because ruff sees normalized names, not the originals. This makes bundles fundamentally less shareable than Patterns (which can be privacy-normalized because they only need to *match*, not *validate*).

A filesystem split enforces the privacy boundary structurally rather than by policy: any future cross-repo sharing reads product fixtures only; repo bundles never leave the repo. This mirrors the existing `recipes/built-in/` (shipped) + `recipes/staged/` (repo-local) precedent.

## Considered Options

- **(A) Repo state only.** Rejected — cold-start: no bundles on day one, promotion gate unsatisfiable.
- **(B) Product fixtures only.** Rejected — no operator-local growth; bundles can't capture repo-specific fix shapes.
- **(C) Hybrid, filesystem-boundaried.** ✅ chosen.

## Consequences

- Two lookup paths: bundle consultation checks product fixtures first (canonical), then repo state (operator-local). Product fixtures win on conflict.
- Cross-repo sharing of repo bundles is structurally prevented. A future feature that wants to share must migrate a bundle from repo state to product fixtures via explicit operator action.
- Product fixtures are populated by the synthesize-then-validate pipeline (alpha.3, ADR-0018), not by manual curation alone. The `ruff-b904` fixture is the first proof-of-concept shipped in alpha.2; alpha.3 shipped the packaging infrastructure. The synthesize-then-validate source pipeline is planned but not yet built.
