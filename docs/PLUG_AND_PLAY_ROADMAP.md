# QA Agent Plug-and-Play Roadmap

## Goal

Make `qa-agent` a truly standalone, reusable QA automation system that a repo owner can deploy on their own repository with minimal manual work, strong safety gates, and no OpenClaw dependency.

## Documentation

See the [Documentation Index](../../docs/README.md) for complete operator guides:
- [Architecture](../../docs/qa-agent-architecture.md) — System design and components
- [Operator Guide](../../docs/qa-agent-operator-guide-autonomous-review.md) — Enabling autonomous review
- [Config Reference](../../docs/qa-agent-config-reference.md) — All configuration options

## Non-goals for now

- CI/CD packaging and public distribution polish
- exhaustive language/plugin coverage in one pass
- one-click installer for non-technical users

## Constraints / assumptions

- Users are repo owners or operators and likely already have core tools installed.
- Missing dependencies must be documented and detected during preflight/onboarding.
- Language/plugin support can expand iteratively in later versions.
- Packaging is a later milestone after core onboarding and safety are solid.

---

## Current state

### Already true
- standalone host-side runtime
- host cron ownership
- local state and health history
- local backend fallback (`auto -> claude -> opencode -> deterministic`)
- standalone test suite
- no OpenClaw execution dependency

### Still blocking true plug-and-play
- onboarding is not smart enough yet
- validation/test/lint/build commands are only weakly inferred
- safety gates are present but not bundled into a dedicated onboarding workflow
- some workspace assumptions still need to be ironed out over time

---

## Delivery strategy: execute in chunks

## Chunk 1 — Preflight + roadmap + portability cleanup
**Objective:** establish a real onboarding gate before execution.

### Deliverables
- `qa-agent preflight --repo /path/to/repo`
- dependency checks
- git/github auth checks
- plugin detection
- language/framework detection
- suggested validation command inference
- safety-clean-tree warning
- roadmap doc
- remove obvious hard-coded plugin workspace assumption in onboarding path

### Acceptance criteria
- a user can assess repo readiness before onboarding
- command works without OpenClaw
- onboarding uses local workspace plugin path rather than hidden OpenClaw path assumption

---

## Chunk 2 — Smart onboarding
**Objective:** make onboarding mostly self-configuring.

### Deliverables
- enrich `onboard` to optionally run preflight first
- infer baseline checks from package manager / repo scripts
- infer discovery mode (`docker` vs host)
- infer backend defaults based on available local tools
- generate safer default config per ecosystem
- surface manual review items clearly

### Acceptance criteria
- new repo onboarding requires little or no manual config editing for standard Python/Node repos
- generated config includes meaningful baseline checks and fallback backend config

---

## Chunk 3 — Safety gates as first-class workflow
**Objective:** make safety explicit and enforceable.

### Deliverables
- onboarding safety policy summary
- dirty-worktree handling policy
- protected branch / merge policy checks
- repo action mode selection (`observe`, `issue-only`, `pr`, `merge`)
- required confirmations for high-risk modes
- optional caps preset profiles (`conservative`, `balanced`, `aggressive`)

### Acceptance criteria
- onboarding makes repo risk posture explicit
- user can choose a safe operating profile before live actions begin

---

## Chunk 4 — Installability and operator ergonomics
**Objective:** improve standalone deployment without full packaging milestone.

### Deliverables
- requirements/install docs
- one-command workspace bootstrap script
- host cron install/update helper refresh
- doctor/preflight/onboard quickstart flow in README
- standalone migration notes from existing repo configs

### Acceptance criteria
- a user can install and validate the tool on a fresh machine with docs alone
- no OpenClaw knowledge required

---

## Chunk 5 — Extensibility and versioned ecosystem growth
**Objective:** expand support without destabilizing the core.

### Prioritization rule
Support the most common repo choices first:
1. TypeScript / JavaScript (Node)
2. Python
3. Go
4. Rust
5. common monorepo/workspace patterns

### Deliverables
- more plugin templates and rule packs
- repo templates by ecosystem
- versioned onboarding heuristics
- migration notes for config schema evolution
- stronger support for common Node/TS and Python repos before broader ecosystem expansion

### Acceptance criteria
- new ecosystem support can be added incrementally
- upgrades preserve backward compatibility for existing onboarded repos
- majority-use-case repos (Node/TS and Python) are easier to onboard than niche stacks

### Implementation detail
See: `docs/CHUNK5_IMPLEMENTATION_PLAN.md`

---

## Proposed command surface end-state

```bash
qa-agent preflight --repo /path/to/repo
qa-agent onboard --repo /path/to/repo --auto
qa-agent doctor --repo my-repo
qa-agent run --repo my-repo --phase issue-cycle --no-dry-run
qa-agent install-cron --repo my-repo
```

---

## Execution notes

- do one chunk at a time
- keep every chunk test-backed
- prioritize standalone behavior over framework integration
- never re-introduce OpenClaw as the execution owner
