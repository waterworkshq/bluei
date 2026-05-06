# Refactor-Class Scaffolding

**Date:** 2026-04-11
**Status:** Implemented for the current lane (classification, persistence, queueing, routing, queue execution, and queue observability are live)

---

## 1. Purpose

This module provides the **first scaffolding slice** for autonomous large-refactor handling in the qa-agent workflow. It establishes:

1. A **finding classification taxonomy** (SIMPLE_FIX / REFACTOR_CLASS / CLAUDE_FIX)
2. A **state machine** (`RefactorPhase`) for tracking multi-phase refactor work
3. **Safety gates** that prevent fully-autonomous execution when files are too large or splits are too many

The specimen used for design is the **ky `test/hooks.ts` max-lines case** (4271 lines → split into `part1-4.ts`).

---

## 2. Taxonomy: `RefactorClass`

Every finding is classified into one of three buckets before any fix engine is invoked:

| Class | Rule examples | Fix engine | Tracking |
|-------|-------------|------------|----------|
| `SIMPLE_FIX` | `ruff-b904` (safe) | Deterministic `apply_autofix()` | Single attempt |
| `REFACTOR_CLASS` | `xo-max-lines`, `xo-complexity` | Claude fix engine + multi-phase | `RefactorPhase` state machine |
| `CLAUDE_FIX` | `type-explicit-any`, `test-coverage-*` | Claude fix engine (single-pass) | Basic `fix_attempts` |

The routing function is `classify_finding(finding) → RefactorClass`.

---

## 3. State Machine: `RefactorPhase`

For `REFACTOR_CLASS` findings, a `RefactorWork` record tracks progress:

```
PLANNING → SPLITTING → VALIDATING → DONE
                                    ↘ ABORTED (safety gate or validation failure)
```

Methods on `RefactorWork`:
- `mark_splitting(targets, original_line_count)` — transition to SPLITTING
- `mark_validating(baseline_fingerprint)` — transition to VALIDATING
- `mark_done()` — transition to DONE
- `mark_aborted(reason)` — transition to ABORTED + sets `needs_human_review=True`

---

## 4. Safety Gates

Two hard limits prevent unbounded autonomous refactors:

| Gate | Limit | Behaviour |
|------|-------|-----------|
| Absolute file size | > 5000 lines (`LARGE_FILE_SAFETY_LIMIT`) | Safety gate triggers → finding routed to human review |
| Split ratio | > 6 target files for `xo-max-lines` | Safety gate triggers → human review required |

The gate function is `can_auto_refactor(finding, worktree_path) → (bool, reason)`.

When triggered, `apply_claude_fix()` returns exit code **3** with the reason string, and the `Finding.refactor_phase` is set to `"aborted"`.

---

## 5. Execution Flow (Current)

### Discovery / issue-cycle

```
discover_findings()
    ↓
classify_finding(finding)
    ↓
if SIMPLE_FIX:
    stays on the normal autofix lane
elif REFACTOR_CLASS:
    route_findings_with_intent(...)
        → persists RefactorWork onto the finding record
        → applies safety gate via can_auto_refactor()
        → oversized / risky cases are enqueued into the refactor queue
        → issue is marked needs-human-refactor-review
elif CLAUDE_FIX:
    stays on the normal Claude-fix lane
```

### Refactor queue execution

```
python3 core/sandbox_local_runner.py --run-phase refactor-cycle --no-dry-run
```

That run path now calls `process_refactor_queue()` and can:
- process approved refactor queue items
- auto-approve pending items when explicitly requested
- limit per-run processing with `--max-queue-items`

Related CLI flags:
- `--run-phase refactor-cycle`
- `--max-queue-items N`
- `--auto-approve`

---

## 6. Files Changed / Added

| File | Change |
|------|--------|
| `core/sandbox_local_runner/reforge.py` | **NEW** — RefactorClass, RefactorPhase, RefactorWork, classify_finding, can_auto_refactor, is_large_refactor, describe_class |
| `core/sandbox_local_runner/refactor_queue.py` | **NEW** — durable queue entries, statuses, enqueue/approve/execute/fail helpers |
| `core/sandbox_local_runner/models.py` | Added `refactor_class` and `refactor_phase` fields to `Finding`; updated `as_dict`/`from_dict` |
| `core/sandbox_local_runner/state.py` | Added `load_refactor_work()`, `save_refactor_work()`, `get_pending_refactor_work()` |
| `core/sandbox_local_runner/orchestrator.py` | Added `route_findings_with_intent()` and `build_refactor_cycle_command()` |
| `core/sandbox_local_runner/lifecycle.py` | Added queue routing / processing helpers including `route_to_human_review()` and `process_refactor_queue()` |
| `core/sandbox_local_runner/cli.py` | Added live refactor-cycle run path and queue-processing flags |
| `core/sandbox_local_runner/__init__.py` | Re-exported routing, persistence, and queue public API |
| `core/sandbox_local_runner/test_reforge.py` | Classification, state machine, safety gate coverage |
| `core/sandbox_local_runner/test_refactor_queue.py` | Queue behavior coverage |
| `core/sandbox_local_runner/test_route_findings_with_intent.py` | Intentional routing coverage |
| `core/sandbox_local_runner/test_refactor_state.py` | RefactorWork persistence coverage |
| `tests/test_refactor_issue_routing.py` | Issue-cycle / non-actionable review-lane regression coverage |
| `tests/test_refactor_queue_processing.py` | Queue execution run-path regression coverage |
| `core/sandbox_local_runner/enforce_architecture.py` | Updated expected modules / legal imports |
| `core/sandbox_local_runner/check_completeness.py` | Updated intentional package-only symbol allowlist |

---

## 7. Remaining Work Beyond This Slice

The core lane is now present and runnable. Remaining work is follow-on improvement, not missing foundation:

- [ ] **Structured split manifests** for large file refactors, so Claude emits planned outputs explicitly
- [ ] **Richer validation fingerprinting** across planning / splitting / validation phases
- [ ] **Human approval UX** beyond raw queue state, for example stronger operator reporting or tighter GitHub surfacing
- [x] **Queue health / observability** in status artifacts and summaries
- [ ] **Deeper end-to-end tests** that exercise a real approved queue item through a successful fix path, not just dry-run / failure-path behavior

See also: `docs/REFACTOR_AUTONOMOUS_DESIGN.md` for the next-phase design contract covering manifest-first planning, fingerprint hardening, operator approval, and rollback behavior.
