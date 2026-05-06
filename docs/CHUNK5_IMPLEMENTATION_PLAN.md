# ⚠️ HISTORIC / LEGACY — See CHANGELOG.md

_This file is retained for archaeological purposes only. The system has evolved significantly since it was written and it does not reflect current architecture or capabilities. See CHANGELOG.md for the authoritative version history._

---

# QA Agent Chunk 5 Implementation Plan

## Objective

Expand `qa-agent` support in a way that improves real-world adoption fast, without destabilizing onboarding or safety.

This chunk is **not** about supporting everything.
It is about supporting the **most common repo types first** so the system becomes immediately useful for the majority of practical use cases.

---

## Strategy

### Principle 1: optimize for the common case
Prioritize ecosystems in this order:

1. **TypeScript / JavaScript (Node)**
2. **Python**
3. **Go**
4. **Rust**
5. **Monorepos / polyrepos with mixed toolchains**
6. Everything else later

Why:
- these cover a very large share of active OSS / indie / startup repos
- current onboarding already leans naturally toward Node + Python
- validation/test/lint/build inference is strongest here
- local fix backends (Claude/OpenCode) are most useful here

### Principle 2: templates before cleverness
Before adding more magical heuristics, add **explicit repo templates** and **ecosystem defaults**.

Reason:
- easier to reason about
- easier to test
- easier to migrate
- easier to version

### Principle 3: version the onboarding brain
Onboarding heuristics should have a version marker so config evolution is tracked instead of silently drifting.

---

## Current plugin baseline

Currently present:
- `plugin-typescript`
- `plugin-python`
- `plugin-test`

So the next step is **not** “build every plugin.”
It is:
- strengthen the existing TypeScript/Python experience first
- add templates and rule packs around them
- then add Go/Rust

---

# Priority rollout plan

## Phase 5.1 — Node / TypeScript first (highest ROI)

### Goal
Make Node/TS repos feel truly plug-and-play for common setups.

### Target repo shapes
- library repos
- API/backend repos
- frontend repos
- single-package repos
- moderate-complexity workspaces

### Supported package managers first
- npm
- pnpm
- yarn

### Supported frameworks first
- plain TypeScript library
- React
- Next.js
- Express / Node backend
- Vitest / Jest test setups

### Deliverables
1. **Node/TS repo template presets**
   - `node-library`
   - `node-api`
   - `react-app`
   - `next-app`
2. **Better validation command inference**
   - `test`
   - `lint`
   - `build`
   - `typecheck`
3. **Workspace detection**
   - detect monorepo/workspace indicators
   - warn when root scripts are aggregator-only
4. **Rule-pack profiles**
   - library-focused
   - frontend-focused
   - backend-focused
5. **Safer Docker/service inference**
   - only when truly relevant
6. **Template-driven onboarding override**
   - allow user to pick a template when inference is ambiguous

### Acceptance criteria
- common TS/JS repos onboard with little or no manual config edits
- inferred baseline checks are usually correct for standard repos
- safety profile remains explicit

---

## Phase 5.2 — Python second

### Goal
Make Python repos nearly as smooth as Node/TS.

### Status
In progress: template wave and package-manager-aware baseline inference.

### Target repo shapes
- library/package repos
- API services
- general application repos

### Tooling priorities
- pytest
- ruff
- mypy (where obvious)
- poetry
- pip
- uv

### Framework priorities
- FastAPI
- Django
- Flask
- generic library/package

### Deliverables
1. **Python repo templates**
   - `python-library`
   - `python-api`
   - `django-app`
   - `fastapi-app`
2. **Validation inference improvements**
   - pytest
   - ruff
   - mypy where config present
3. **Dependency manager awareness**
   - poetry vs pip vs uv
4. **Rule-pack profiles**
   - backend/API profile
   - package/library profile
5. **venv / environment guidance**
   - avoid assuming global Python tools are enough

### Acceptance criteria
- standard Python repos get useful validation defaults immediately
- API/service repos infer safer defaults than library repos

---

## Phase 5.3 — Go and Rust

### Goal
Add high-value statically typed ecosystems with relatively predictable tooling.

### Go priorities
- `go test ./...`
- module detection
- common layout handling

### Rust priorities
- `cargo test`
- `cargo check`
- optional `cargo clippy`

### Deliverables
- `go-service` template
- `rust-crate` template
- baseline checks for common cases
- initial rule packs / discovery policies

### Acceptance criteria
- Go and Rust repos can onboard with safe usable defaults
- manual edits are minimized for common structures

---

## Phase 5.4 — Monorepo support

### Goal
Handle the most common monorepo patterns without pretending to solve every weird setup.

### Priorities
- pnpm workspace
- yarn workspace
- npm workspaces
- Python multi-package repo (light support only)

### Deliverables
1. **Monorepo detection**
2. **Root-vs-package execution strategy**
3. **Scoping guidance**
   - onboard root repo vs onboard package path
4. **Monorepo template presets**
   - `node-workspace-root`
   - `node-package-in-workspace`
5. **Review warnings**
   - if inferred root commands are too broad/costly

### Acceptance criteria
- common JS/TS monorepos don’t onboard with obviously wrong root commands
- user gets clear guidance when package-level onboarding is safer than root-level onboarding

---

# Cross-cutting architecture work

## A. Repo templates

Introduce first-class template presets:

```text
templates/repos/
  node-library.yaml
  node-api.yaml
  react-app.yaml
  next-app.yaml
  python-library.yaml
  python-api.yaml
  django-app.yaml
  fastapi-app.yaml
  go-service.yaml
  rust-crate.yaml
```

Each template should define:
- baseline checks
- safety defaults
- discovery defaults
- limits defaults
- suggested rule-pack

Templates should be:
- explicit
- testable
- versionable

---

## B. Rule packs

Introduce reusable rule-pack presets by repo type:

Examples:
- `node-library-safe`
- `node-frontend-safe`
- `node-backend-safe`
- `python-api-safe`
- `python-library-safe`
- `go-safe`
- `rust-safe`

These should control:
- enabled rules
- disabled rules
- severity preferences
- quick-win biases

---

## C. Onboarding heuristics versioning

Add config metadata like:

```yaml
meta:
  onboarding_version: 2
  template: node-library
  inferred_by: auto
```

Purpose:
- track what logic produced the config
- allow future migration tooling
- prevent silent behavioral drift

---

## D. Config migration policy

When config shape evolves:
- never silently drop fields
- infer missing fields conservatively
- keep migration notes in docs
- add tests for old-config compatibility

---

# Suggested implementation order

## Step 1 — strengthen existing ecosystems
Start here before adding new languages.

1. add template system
2. add Node/TS templates
3. add Python templates
4. add onboarding metadata/version fields
5. update onboarding to prefer template match when confidence is high

## Step 2 — improve inference quality
1. package-manager-specific command generation
2. framework-aware baseline checks
3. workspace/monorepo detection
4. safer Docker inference

## Step 3 — add next ecosystems
1. Go
2. Rust

## Step 4 — expand templates/rule packs
Only after the template/config/version pattern is stable.

---

# What to defer deliberately

Not now:
- Java
- Ruby
- PHP
- highly custom enterprise monorepos
- Kubernetes/environment-aware deployment inference
- cloud-provider-specific logic
- public packaging/distribution polish

These are valid later, but not part of the 80/20 path.

---

# Acceptance definition for Chunk 5

Chunk 5 is successful when:

1. standard Node/TS repos onboard with strong defaults
2. standard Python repos onboard with strong defaults
3. repo templates exist and are versioned
4. onboarding records what template/version produced a config
5. config migration remains backward compatible
6. common users can get to a safe first run faster than they can manually author config

---

# Recommended next execution chunk

Execute Chunk 5 in this order:

### Chunk 5A
- template system
- onboarding metadata/version fields
- Node/TS templates

### Chunk 5B
- Python templates
- Python inference refinement

### Chunk 5C
- Go/Rust templates
- initial support

### Chunk 5D
- monorepo handling
- rule-pack presets

This keeps the rollout small, testable, and high-impact.
