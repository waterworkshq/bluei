# Blenny Brand Identity

**Product:** Blenny — The Cleaner. Echo killer. Finding suppressor.
**Family:** Waterworks HQ (Orcy + Blenny)
**Codebase:** `qa-agent`

---

## Name

- **Blenny** (4 letters, one syllable — shorthand for *blennyalopod*)
- Lowercase in product reference: "run blenny", "blenny cleans"
- Capitalized as **Blenny** at start of sentences and in headings

### Inspiration

Octopuses are blennyalopods — deep-sea creatures that move silently, ink when threatened, reach into every crack with countless arms, and disappear without a trace. Blenny dives into repos the same way: reaches into every file, inks the findings into PRs, and leaves only clean water behind.

The name **Blenny** honors this: eight arms, infinite reach, zero noise.

---

## Tagline

> **When Blenny runs, problems die.**

Alternative / sub-taglines:
- **Silence from the depths.**
- **Dive deep. Come up clean.**
- **The echo killer.**

---

> **Keep your waters clean.**

Short-form: **Ink and dust.**

Alternative / sub-taglines:
- **Silence from the depths.**
- **When Blenny runs, problems die.**
- **Dive deep. Come up clean.**
- **The echo killer.**

---

## CLI Verbs

```
blenny init        — onboard a new machine
blenny run         — execute a cycle (ink, scan, duster)
blenny scan        — analyze a tank (alias for run --phase issue-cycle)
blenny ink         — create a findings PR (alias for run --phase pr-cycle)
blenny doctor      — diagnose system health
blenny status      — show tank health overview
blenny preflight   — assess a tank before onboarding
blenny onboard     — add a tank to Blenny's care
blenny heal        — auto-clean transient artifacts
blenny report      — generate health report (PDF / text)
blenny install-cron — set up scheduled cycles
```

### Lexical mapping (converged with Green)

| Blenny term | Maps to | Meaning |
|-----------|---------|---------|
| scan | analyze | Blenny scans a tank for findings |
| ink | PR/submit | Blenny inks findings into a pull request |
| duster | dry-run | Blenny dusts before committing |
| tank | repo | A tank is where Blenny lives, distinct from Orcy's habitat |
| dive | cycle start | Blenny dives into the tank |
| purge | issue fix run | Each cycle is a purge operation |
| lock | claim | Blenny locks onto findings |

---

## Design System: Abyssal Blenny

A dark, deep-sea aesthetic paired with Ocean Orcy's tokens. Blenny lives in the same ocean as Orcy — just deeper, darker, quieter.

### Color Tokens (Abyssal variant of Ocean Orcy)

| Token | Hex | Role |
|-------|-----|------|
| abyss | `#060a0e` | Page background (shared with Orcy) |
| deep | `#0d1824` | Surface background (shared with Orcy) |
| trench | `#091420` | Blenny-specific deeper panels |
| ink | `#111a24` | Elevated panels (shared with Orcy's whale) |
| blowhole | `#e4ecf4` | Primary text (shared with Orcy) |
| echo | `#1e4858` | Links, active states (shared with Orcy) |
| biolum | `#00d4aa` | Blenny accent — teal bioluminescence |
| dorsal | `#2a5f73` | Highlights (shared with Orcy) |
| breach | `#fa746f` | Errors, CTA (shared with Orcy) |
| krill | `#f0c060` | Warnings (shared with Orcy) |
| ink_cloud | `#1a1a2e` | Blenny-specific shadow/overlay |

**Key difference from Orcy:** Blenny swaps Orcy's `echo`-heavy palette for deeper, darker tones with bioluminescent accent (`#00d4aa`) — like a squid's glow in the abyss.

### Typography

- **Headings:** Space Grotesk (shared with Orcy for family consistency)
- **Body:** Manrope (shared)
- **Code:** JetBrains Mono (shared)

---

## Mascot Concept

A stylized **octopus** — tentacles reaching downward into the deep, bioluminescent tips glowing. (Converged on octopus over squid — more arms, more tools, more actions that map to Blenny's operations.)

Form should suggest:
- **Depth** — body positioned as if diving
- **Reach** — tentacles extending in multiple directions, pulling findings from every corner
- **Ink** — subtle ink-cloud silhouette behind, vanishing as it releases
- **Eyes** — calm, all-seeing, unblinking — the octopus is always watching the tank

Style: Minimal vector, similar to Orcy's rounded orca. Same circular badge format for consistency.

For differentiation from Orcy's orca:
- Orcy = horizontal, hunting forward, breach-up energy (orca breaching)
- Blenny = all-directional, reaching patient, depth-first energy (octopus tending the reef)

---

## Voice & Terminology

### Product voice

**Blenny doesn't talk much. When it does, it matters.**

- Quiet, methodical, precise
- Uses ocean/depth/echo metaphors
- Avoids hype — Blenny's job is to make things boring (in a good way)
- When everything is clean, Blenny says nothing

### Terminology map

| Orcy term | Blenny equivalent | Notes |
|-----------|----------------|-------|
| Hunt | Dive | Blenny dives into tanks |
| Habitat | Tank | Where Blenny lives, doesn't own (distinct from Orcy's habitat) |
| Pod | — | Blenny is a solo cleaner (for now) |
| Mission | Purge | Each cycle is a purge operation |
| Claim | Lock | Blenny locks onto findings |
| Surface | Breach | Shared — findings breach into PRs |
| Breach | Breach | Shared |
| Echo | Echo | Shared — Blenny kills echoes |
| — | Ink | PR creation — an octopus inks and disappears |
| — | Depth | Severity / scan intensity |
| — | Duster | Dry-run mode — kicks up dust before settling |
| — | Scan | Analysis pass over a tank |
| — | Reach | Tentacle metaphor for multi-file coverage |

### Copy patterns

- "Blenny found 0 findings." — anything else implies noise
- "No echoes to surface." — the clean state
- "Ink and dust." — after a clean operation (CLI footer)
- "Diving tank_name..." — cycle start
- "Scanning depth: full" — exhaustive scan mode
- "Scanning depth: surface" — quick scan mode
- "Inking findings..." — PR creation phase
- "Duster pass complete." — dry-run success

---

## Logo concept

Circular badge with:
- Deep background (`#091420` trench)
- Octopus silhouette in `#00d4aa` biolum
- Tentacles extending beyond the badge edge, reaching in multiple directions
- "blenny" wordmark at bottom, same arc format as Orcy
- Ink cloud behind the octopus — subtle, like it's already vanished

## Domain

Blenny will get its own domain (e.g. blenny.dev) once the codebase is mature enough to stand alone.

**Timeline:** maturity-gated, not date-gated. Same model as Orcy's launch.

Until then, Blenny lives in the workspace repo as the `qa-agent` codebase, ready to be extracted when the code earns the right to surface on its own.

**File location:** `design_assets/blenny-logo.svg` (create when ready)

---

## Relationship to Orcy

| Dimension | Orcy | Blenny |
|-----------|------|------|
| Role | Pod coordinator | Tank cleaner |
| Metaphor | Orca hunting pod | Octopus tending the reef |
| Direction | Horizontal, forward | All-directional reach |
| Energy | Coordinated, active | Patient, thorough, meticulous |
| Output | Missions surface as PRs | Findings surface as silence |
| Tagline (proposed) | "Hunt as a pod." | "Keep your waters clean." |
| Color accent | `#1e4858` (echo) | `#00d4aa` (biolum) |

Together they form **Waterworks HQ** — the ecosystem for AI-assisted development. Orcy orchestrates; Blenny maintains. One hunts; one cleans.

---

*Brand file v1.2 — 2026-05-09*
*All decisions locked. Sound: keep your waters clean ✅ | octopus mascot ✅ | own domain (maturity-gated) ✅*
*Part of Waterworks HQ*

---

## Implementation Priority (per Sound, 2026-05-09)

> "Don't worry about the branding for now. Focus on the code before all of that. The actual use is what counts."

Branding is done for now. These docs serve as the reference when the codebase matures enough to surface on its own domain.

**Current priority:** Tighten the CLI, make the tank concept real in code, mature the `blenny run` / `blenny scan` / `blenny ink` flow. One clean cycle at a time.
