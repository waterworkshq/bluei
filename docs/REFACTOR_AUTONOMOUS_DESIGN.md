# Autonomous Large-Refactor Design

**Date:** 2026-04-11
**Status:** Proposed next-phase design on top of the live refactor-cycle lane
**Specimen case:** `ky/test/hooks.ts` max-lines split

## 1. What already exists

The current refactor-cycle lane is no longer hypothetical. It already provides:

- `REFACTOR_CLASS` routing for structural findings
- durable `RefactorWork` persistence on findings
- durable refactor queue state
- `refactor-cycle` CLI execution
- human approval gating
- queue counts in status artifacts
- baseline architecture and queue-processing tests

That means the next step is not "invent refactor-cycle". It is to make the current lane capable of executing larger structural work with better planning, validation, and operator visibility.

## 2. Problem statement

Today, qa-agent can intentionally route large structural findings away from the generic autofix loop, but it still lacks the machinery to execute those refactors with strong guarantees.

Current gaps:

1. planning output is implicit instead of structured
2. validation identity is too coarse across multi-phase rewrites
3. operator approval is queue-centric rather than plan-centric
4. successful end-to-end approved execution is not deeply covered
5. rollback and re-entry behavior are underspecified for partial splits

## 3. Design goals

The next autonomous large-refactor capability should:

- keep large refactors out of generic retry exhaustion
- make the plan explicit before code is rewritten
- preserve behavioral safety through deterministic validation gates
- give humans a reviewable plan instead of a blind yes/no queue item
- support resumable multi-phase execution without losing provenance
- fail closed when evidence is incomplete

## 4. Proposed state model

Extend `RefactorWork` from a lightweight phase tracker into a plan-carrying state record.

### New fields

- `plan_fingerprint`: stable hash of the approved plan
- `source_fingerprint`: hash of the original file set before edits
- `target_manifest`: explicit output manifest for split/merge operations
- `validation_manifest`: named checks required for this refactor
- `change_budget`: max files changed, max LOC delta, max generated files
- `rollback_strategy`: how to restore or abandon partial output safely
- `execution_attempts`: count of actual execution attempts
- `artifacts`: prompt file, manifest file, validation reports, diff stats

### Refined phases

```text
PLANNING
  -> PLAN_REVIEW
  -> SPLITTING
  -> VALIDATING
  -> LANDING
  -> DONE

Any phase
  -> ABORTED
```

`PLAN_REVIEW` is the key addition. Human approval should approve a concrete plan payload, not just a queue entry.

## 5. Structured split manifest

For large-file refactors, require an explicit manifest before edits begin.

### Manifest shape

```json
{
  "finding_id": "...",
  "source_files": ["test/hooks.ts"],
  "outputs": [
    {
      "path": "test/hooks/part1.ts",
      "purpose": "setup and shared fixtures",
      "expected_max_lines": 1500,
      "exports": ["..."]
    }
  ],
  "shared_moves": [
    {
      "symbol": "makeHookFixture",
      "from": "test/hooks.ts",
      "to": "test/hooks/shared.ts"
    }
  ],
  "validation": {
    "checks": ["xo target files", "ava targeted suite"],
    "must_preserve_exports": true,
    "must_preserve_import_graph": true
  }
}
```

### Why this matters

Without a manifest, the split is only implied by the model output. That makes review, validation, resumability, and rollback much weaker.

## 6. Validation fingerprinting

Validation should compare more than "checks passed".

### Proposed fingerprints

- `source_fingerprint`: original file contents + tracked sibling file set
- `plan_fingerprint`: approved manifest + rule + target budget
- `validation_fingerprint`: normalized validation command set
- `result_fingerprint`: output file contents + preserved symbol/export map

### Rules

- execution must abort if source changes after plan approval
- execution must abort if manifest changes without new approval
- validation must record both commands run and normalized outputs
- successful completion must bind the result to the approved plan fingerprint

## 7. Approval UX

Queue approval should become plan approval.

### Current

- operator sees pending queue item
- approval is detached from exact planned outputs

### Proposed

Each pending refactor item should expose:

- finding summary
- source file size and risk classification
- explicit target manifest
- expected files created/changed
- validation commands to be run
- change budget
- rollback note

Approval outcome options:

- `approve_plan`
- `approve_with_constraints`
- `reject_plan`
- `request_manual_takeover`

## 8. Execution contract

Once approved, execution should follow a bounded contract:

1. verify source fingerprint still matches
2. materialize prompt + manifest artifact
3. run split/refactor in isolated worktree
4. run structural validation
5. run repo-specific checks from validation manifest
6. compute diff stats against approved budget
7. either land and mark `DONE`, or abort with preserved artifacts

## 9. Rollback and failure handling

Large refactors should fail closed.

### Abort triggers

- source drift after approval
- manifest mismatch
- target file explosion beyond budget
- missing expected outputs
- validation mismatch
- semantic check failure

### Abort behavior

- move queue item to `aborted`
- preserve manifest, prompt, diff summary, and validation logs
- mark `needs_human_review=True`
- do not silently retry unless the plan is regenerated

## 10. Suggested implementation slices

### Slice A: manifest-first planning

- add `target_manifest`, `plan_fingerprint`, `source_fingerprint`
- write manifest artifacts to disk
- require approved items to carry a manifest

### Slice B: validation hardening

- add validation/result fingerprints
- record normalized check outputs
- abort on source or plan drift

### Slice C: operator reporting

- surface manifest and budgets in status/reports
- improve human approval visibility beyond raw queue state

### Slice D: real approved-path test

- add an end-to-end test that runs:
  - queue item creation
  - approval
  - manifest-backed execution
  - successful completion

## 11. Recommended next build order

1. **Structured split manifest**
2. **Fingerprint hardening across plan/source/result**
3. **Operator approval/reporting improvements**
4. **Approved-path end-to-end execution test**

That order gives the highest safety return first.

## 12. Acceptance for this design task

This design is complete when the repo has a concrete next-phase contract covering:

- task classification boundary
- plan-first execution model
- bounded decomposition strategy
- validation gates and fingerprints
- state-machine evolution
- landing path
- rollback rules

This document is that contract.
