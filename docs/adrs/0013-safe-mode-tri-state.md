# ADR-0013: Safe-mode is a single tri-state `learning.mode` flag, composing as a ceiling over per-asset-class policy

**Status:** accepted

The Operator Control Plane ships with a single tri-state kill switch — `learning.mode` — taking values `active` | `audit_only` | `paused`. It composes as a **ceiling** over the per-asset-class policy (ADR-0007), not a replacement: the mode caps what policies can authorize, it does not redefine them.

| Mode | Dry Replay | Auto-promote (gate_open policy) | Auto-demote (SPRT) | Gate-closed promotions | Operator actions |
|---|---|---|---|---|---|
| `active` | runs | proceeds + audit | fires | queued in inbox | all enabled |
| `audit_only` | runs | held (forced gate_closed) | runs but does not fire | queued in inbox | all enabled |
| `paused` | does not run | held | does not run | queued but not processed | all enabled (so operator can still resume when ready) |

## Why tri-state, not granular flags

Seed 10 listed granular switches (`no_promotion`, `no_demotion`, `dry_replay_only`, …). Rejected: granularity invites misconfiguration. An operator who sets `no_promotion=true` and forgets `no_demotion` gets a broken Pattern that keeps firing — exactly the footgun the kill switch exists to prevent. Panic buttons must be one knob.

## Why three states, not two

The three map to real operator scenarios that a binary "on/off" collapses:

- `active` — normal autonomous operation.
- `audit_only` — the shadow-evaluation posture ("I want to see what *would* happen before I trust this"). Dry Replay still runs, SPRT still computes, but nothing fires. This is the natural posture for a repo onboarding to the substrate for the first time. Matches how bluei already uses shadow modes in review/.
- `paused` — full freeze ("something went wrong on another repo, stop everything while I investigate"). No Dry Replay spend, no decisions, evidence frozen. Recovery is operator-initiated resume.

## Considered Options

- **(A) Tri-state `learning.mode` flag.** ✅ chosen.
- **(B) Granular flags (`learning.no_promotion`, etc.).** Rejected — misconfiguration risk; not a real panic button.
- **(C) Out of scope; achieve via per-asset-class policy + SPRT α=1.0.** Rejected — technically possible but operationally dishonest. "Freeze learning by editing 4 YAML keys across 2 sections and tuning a Greek letter" is homework, not a kill switch. A release named "Safe Learning" must ship with a kill switch.

## Consequences

- The flag lives in repo config (`repos/<name>/config.yaml`) under a `learning:` key, alongside the per-asset-class policy and SPRT inputs. Global default (workspace config) is `active`; per-repo can override.
- `audit_only` is the recommended onboarding posture for repos new to the substrate: run a few cycles, inspect the inbox and Dry Replay outcomes, then flip to `active`. Documentation should call this out.
- The dashboard renders the current `learning.mode` prominently so operators don't forget they left the system paused.
- Composition rule: mode is a ceiling, not a union. If policy says `gate_open_audit` but mode is `audit_only`, the effective behavior is `gate_closed`. The per-asset-class policy is the operator's intent; the mode is the operator's brake.
