# Advanced Care Capability Matrix

Date: 2026-04-09
Status: Post-hardening audit after merge-cycle and capability-gap fixes

## Purpose

Capture the difference between:
- declared capability in catalog/config/docs
- structurally wired capability in discovery + remediation paths
- live-proven capability in real repos

This is the activation reference for expanding qa-agent into advanced repo healthcare.

---

## Current Summary

### Strong / live-proven
- Review-care lifecycle on managed PRs
- Autonomous merge-care (single-merge-per-run, deterministic ordering, conflict triage)
- LLM-assisted `ruff-b904`
- LLM-assisted `ruff-s311` (lighter proof, but working)
- Deterministic `ruff-c408` (partial but real)
- Simple lint hygiene (`xo-no-warning-comments` at least lightly proven)

### Structurally wired, but not yet deeply live-proven
- `xo-max-lines`
- `xo-complexity`
- `type-explicit-any`
- `type-missing-return`
- `type-missing-param`
- `test-coverage-branch`
- `test-coverage-function`

### Previously dormant, now wired
- `type-untyped-import`
- `test-coverage-line`

### Still requires caution
- broad autonomous refactor rollout on `ky`
- any rollout that mixes refactors + routine lint fixes + review remediation all at once
- high-volume repos where issue-cap saturation can prevent meaningful forward progress

---

## Rule Family Matrix

| Rule / Family | Declared | Discoverable | Fix Path | Live-Proven | Current Readiness | Notes |
|---|---|---:|---|---:|---|---|
| `ruff-b904` | yes | yes | LLM | yes | ready | strongest non-deterministic live lane |
| `ruff-s311` | yes | yes | LLM | light | guarded-ready | low sample size |
| `ruff-c408` | yes | yes | deterministic | partial | ready | backlog still large |
| `ruff-b007` | yes | yes | deterministic | weak | guarded-ready | not much recent evidence |
| `ruff-e501` | yes | yes | deterministic | weak | low-priority | very high-volume style noise |
| `xo-no-warning-comments` | yes | yes | deterministic | light | ready | small/simple lane |
| `xo-max-lines` | yes | yes | Claude | weak | guarded rollout | now normalized from legacy `max-lines` |
| `xo-complexity` | yes | yes | Claude | weak | guarded rollout | now normalized from legacy `complexity` |
| `type-explicit-any` | yes | yes | deterministic | weak | guarded rollout | may need stronger validation context |
| `type-missing-return` | yes | yes | Claude | weak | guarded rollout | declared Claude-required |
| `type-missing-param` | yes | yes | deterministic | weak | guarded rollout | watch false positives from compiler output |
| `type-untyped-import` | yes | yes | LLM | no | pilot only | newly wired from TS7016-like output |
| `test-coverage-branch` | yes | yes | Claude | weak | pilot only | coverage generation/report availability matters |
| `test-coverage-function` | yes | yes | Claude | weak | pilot only | same as above |
| `test-coverage-line` | yes | yes | LLM | no | pilot only | newly wired, keep coarse to avoid spam |

---

## Important Fixes Landed In This Audit Window

### Merge and review care
- merge-cycle now merges at most one PR per run
- merge ordering is deterministic oldest-first
- `DIRTY` / `BEHIND` PRs are triaged back to `pr-cycle`
- autonomous review artifacts can satisfy merge gating when they are explicitly quiet and `merge_ready`

### Capability truthfulness
- legacy ky rule names now normalize into active `xo-*` remediation flow
- `type-untyped-import` is no longer catalog-only; it is discovered from TS compiler output
- `test-coverage-line` is no longer catalog-only; it is discovered from coverage statement maps
- both above rules are now routed into LLM fix prompts

---

## Repo-Specific Read

### Zulip
Operationally strongest lanes today:
- Python linter discovery
- `ruff-b904`
- review-care / merge-care

Primary practical constraint:
- issue-cap saturation and queue pressure, not missing merge mechanics

### ky
Main caution:
- advanced-care exists structurally but remains lightly proven in live repo flow
- external discovery / historical rule-name drift suggests rollout should be deliberate

---

## Recommended Activation Order

### Phase 1: enable first
1. `xo-complexity`
2. `xo-max-lines`

Policy:
- max 1 active refactor PR per repo
- stricter validation than routine lint fixes
- separate reporting bucket from normal hygiene

### Phase 2: narrow pilot
3. `type-missing-return`
4. `type-explicit-any`
5. `type-missing-param`

Policy:
- only on repos with stable baseline checks
- watch validation-failure rates and noop churn

### Phase 3: experimental
6. `test-coverage-function`
7. `test-coverage-branch`
8. `type-untyped-import`
9. `test-coverage-line`

Policy:
- pilot on one repo at a time
- require strong local test conventions
- avoid combining with large refactor backlog at first

---

## Operational Rules For Advanced Care

- keep refactor-care separate from routine bug/lint care in summaries
- one active advanced-care PR per repo at a time
- do not allow multiple refactor merges in a single cycle
- prefer maintainability wins with strong validation over broad test-generation churn
- if queue saturation prevents movement, reduce discovery pressure before widening capability

---

## Exit Criteria Before Broad Enablement

Broad advanced-care rollout is justified when:
- at least 3 to 5 successful live PRs land for the selected advanced rule family
- no sustained spike in validation-failed / scope-limit-exceeded states
- merge queue remains stable after refactor PRs start landing
- issue backlog remains actionable instead of cap-saturated
