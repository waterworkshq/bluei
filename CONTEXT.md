# bluei — Domain Context

The shared vocabulary for the bluei QA agent codebase. Architectural discussions, code review, and planning documents should use these terms exactly.

## Language

**Finding**
A detected issue in a target repo — lint error, type violation, style problem, test gap, or any rule-triggered observation. Persisted in `findings.jsonl`.
_Avoid_: issue (reserved for the GitHub issue tracking wrapper), defect, bug, error.

**Issue**
A tracked GitHub issue created from one or more Findings. Wraps a Finding with lifecycle state (status, history, escalation tracking). Persisted in `issues.json`.
_Avoid_: ticket, finding (a Finding becomes an Issue, but they are distinct).

**Cycle**
A single run phase. Five types: issue-cycle (discovery), pr-cycle (fix + PR), merge-cycle (auto-merge), orchestrated (full pipeline), review-cycle (PR review).
_Avoid_: run (ambiguous — a run may contain multiple cycles), pass, sweep.

**Care Level**
How autonomous bluei can be for a repo. Four tiers: watch-only, note-only, offer-fixes, full-care.
_Avoid_: mode (too generic), autonomy level, trust level.

**Pattern**
A learned fix extracted from a successful diff. Stored with confidence scores and structural hashes. Replayed deterministically on future matching Findings.
_Avoid_: template (reserved for onboarding templates), recipe (reserved for declarative YAML fixes).

**Recipe**
A declarative YAML fix definition — text replacement, regex substitution, or command execution. Simpler than a Pattern; authored, not learned.
_Avoid_: rule (reserved for detection rules), fix template.

**Campaign**
A multi-phase batch of related fixes. Groups Findings by rule or path, orders by dependency, and executes phases sequentially.
_Avoid_: batch (reserved for the batch PR engine), plan (too generic).

**Emergent Rule**
A new detection rule proposed from repeated Finding patterns. Lifecycle: PROPOSED → CANDIDATE → TENTATIVE → ACTIVE → RETIRED/REJECTED.
_Avoid_: custom rule, dynamic rule.

**Directive Seeding**
The feedback loop that injects prior fix attempts, lessons, and failure patterns into LLM prompts so the engine avoids repeating failed fixes.
_Avoid_: context injection (too generic), prompt stuffing, lesson loop.

**Reforge**
The routing step that classifies Findings as refactor candidates and sends them to the refactor queue for human-gated processing.
_Avoid_: refactor engine (too broad — reforge is specifically the classification + routing step).

**Worktree**
An isolated git worktree used for applying fixes without touching the main working directory. Used during fix application, validation, and review.
_Avoid_: branch (a worktree is backed by a branch but is a filesystem-level concept), sandbox.

**Validation Gate**
The post-fix check suite that confirms a fix doesn't introduce regressions — lint, type-check, test, and smoke-test pass.
_Avoid_: CI (external concept), verification (too close to verify_fix_closed).

**Escalation**
A structured event recorded when a cycle fails, merge fails, dedup saturation is detected, or rebase conflict trends increase. Written to `escalation_log.jsonl`.
_Avoid_: alert (reserved for notifications), incident.

## Flagged ambiguities

- **"State"** is overloaded. `engine/state.py` provides bare functions for file I/O. `app/state.py` provides a StateManager class. When discussing state, specify which layer or say "StateManager" vs "engine state functions."
- **"Escalation"** exists in both `engine/escalation.py` (pattern detection + structured logging) and `app/escalation.py` (thin wrapper + status checking). The engine module is the real implementation; the app module is a pass-through.

## Example dialogue

> **Dev:** "The pr-cycle found 12 Findings. Three were classified as refactor-class by Reforge, so they went to the refactor queue. The remaining 9 went through the deterministic cascade — 4 matched Recipes, 2 matched Patterns, 3 fell through to Directive Seeding and got LLM fixes."
>
> **Domain expert:** "Good. Did the Validation Gate pass for all 6 deterministic fixes? And did the Campaign for the rust-rule batch finish its second phase?"
>
> **Dev:** "Yes — all passed. The Campaign is paused at phase 2 because of an Escalation: dedup saturation on the `clippy::needless_pass` rule. The Care Level for that repo is offer-fixes, so it won't auto-merge anyway."
>
> **Domain expert:** "Right. Lift the suppression on that rule and re-run the pr-cycle. The Emergent Rule for that pattern is in TENTATIVE — check if the shadow scan has enough evidence to promote it."
