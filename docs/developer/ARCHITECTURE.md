<!-- CATEGORY: developer -->

# bluei — Architecture

## Overview

bluei is an autonomous QA agent for GitHub repositories. It scans code for issues,
fixes them, opens pull requests, and can auto-merge — all configurable by care level.

> **Terminology:** bluei uses branded terminology in user-facing output. `brand.py`
> maps internal terms to brand language: `finding→speck`, `health_score→vitality`,
> `safety_mode→care_level`, `repo→project`, `onboard→register`, `baseline→starting point`,
> `snapshot→check-in`. This document uses internal terms for developer clarity.

```
  bin/bluei (shebang)
       ↓
  bin/bluei.py (CLI dispatcher: ~27 commands)
       ↓
  bluei/app/ (core app: runner, health, registry, onboarding, reports, etc.)
       ↓
  bluei/engine/ (execution engine: discovery, fixing, PR lifecycle, patterns)
       ↓
  plugins/ (language detection: python, typescript, go, rust, shell, docker, markdown)
```

The canonical Python package is `bluei`. Runtime code is organized under `bluei.app`, `bluei.engine`, `bluei.review`, `bluei.campaigns`, and `bluei.common` (cross-layer shared types, sits between engine and app/review).

## Layers

### 1. CLI Layer (`bin/`)

The `bluei` shebang script resolves the repo root via `os.path.realpath(__file__)`,
bootstraps the Python venv if needed, then delegates to `bin/bluei.py`.

`bin/bluei.py` is a smart dispatcher that handles:

- Venv auto-bootstrap on first run
- Command routing: maps CLI verbs to engine calls
- Interactive init wizard
- Help text for all commands

### 2. App Layer (`bluei/app/`)

Manages the operational surface: repository onboarding, health scoring, PR review,
report generation, campaign orchestration, and emergent rule discovery.

| Module | Responsibility |
|--------|---------------|
| `runner.py` | Wraps `bluei.engine` via subprocess. Cycle orchestration. |
| `config.py` | Configuration management, workspace resolution. |
| `registry.py` | YAML-based repo registry with CRUD. |
| `bluei.app.onboarding/` | Repository onboarding package: detection, inference, templates, engine (5-module split per rec-22). |
| `health.py` | Health score engine (0-100). Aggregates findings, test coverage, CI status. |
| `bluei.review.cycle` | GitHub-native PR review lifecycle. `ReviewCycleEngine` orchestrator composed from 9 focused mixins (Safety, Worktree, Generation, Observation, StatusClassification, StateRecording, PrTargeting, Publication, Autonomous). |
| `report.py` | Report generation in text, PDF, HTML, JSON, and WhatsApp formats. |
| `dashboard.py` | Observability dashboard HTML generator with SVG sparklines. |
| `bluei.campaigns.planner` | Groups findings into ordered phases for batch execution. |
| `bluei.campaigns.executor` | Walks campaign phases, executes fixes per phase. |
| `bluei.campaigns.state` | Persistent state for campaign plans. |
| `emergent_rules.py` | Proposes new detection rules from repeated finding patterns. |
| `healer.py` | Self-heal: auto-cleans stale locks, worktrees, pycache, registry GC. |
| `escalation.py` | Structured JSONL log for cycle failures, merge failures, dedup saturation. |
| `auto_tune.py` | Adaptive threshold tuning from telemetry patterns. |
| `cycle_signals.py` | Cross-cycle awareness: bridges state between consecutive cycles. |
| `plugins.py` | Discovers and loads language plugins from `plugins/`. |

### 3. Execution Engine (`bluei/engine/`)

The execution engine (~50 modules plus 6 subpackages: `ast_engine/`, `commands/`,
`notifiers/`, `recipes/`, `scaffold/`, `structural_hash/`). Handles discovery,
fixing, PR management, batch operations, and pattern learning.

| Module | Responsibility |
|--------|---------------|
| `cli.py` | Main CLI: `argparse`-based command dispatcher for the internal runner. |
| `orchestrator.py` | Cycle orchestrator: builds command pipelines, `discover_findings()`, runs cycles. |
| `lifecycle.py` | Fix lifecycle: `apply_autofix`, validation gates, git ops, directive seeding. |
| `batch_pr.py` | Batch PR engine: groups findings into batches, manages multi-fix PRs. |
| `gh/` | GitHub API abstraction package (decomposed from former `gh.py` god module): `_core.py`, `repo.py`, `sandbox.py`, `issue_ops.py`, `pr_ops.py`, `merge_eval.py`, `pr_regression.py`, `live_counts.py`. PR creation, issue management, repo config. |
| `linters.py` | Linter integration: ruff, xo, staticcheck, shellcheck, etc. |
| `constants.py` | Catalog of detection rules, tool paths, cooldown configs, cost rates. See [RULES_REFERENCE.md](../reference/RULES_REFERENCE.md) for the full rule catalog. |
| `cascade.py` | Multi-engine cascade: tries deterministic fixes before falling to LLM. |
| `prompts.py` | LLM prompt rendering with directive seeding context injection. |
| `structural_hash/` | AST-based structural hashing package (split per S4: `__init__.py`, `python.py`, `text.py`). |
| `recipe_engine.py` | Declarative YAML fix recipe system. See [RULES_REFERENCE.md](../reference/RULES_REFERENCE.md#recipes) for all built-in recipes. |
| `recipe_handlers.py` | Text, regex, and command-based recipe handlers. |
| `pattern_store.py` | Persistent store for learned fix patterns. `open_pattern_store()` factory is the canonical production construction path — loads product-fixture seeded patterns into the in-memory index after construction. |
| `pattern_extractor.py` | Learns fix patterns from successful git diffs. Routes through `register_asset()` (the producer interface). |
| `pattern_replay.py` | Matches findings to stored patterns and applies deterministically. |
| `asset_registry.py` | `register_asset()` — centralized runtime insertion path (dedup + source stamp). The producer interface for pattern extraction and future alpha.4 Recipe Foundry / Rule Hatchery. |
| `seeded_pattern_loader.py` | Loads product-fixture seeded patterns from `seeded_patterns/` into the store index (called by `open_pattern_store`). Product wins on structural-hash conflict. |
| `guideline_loader.py` | Loads authoritative guidelines (PEP 8, OWASP, TS conventions) from `authoritative_guidelines/` into LLM fix prompts by rule family. NOT governed (ADR-0017). |
| `rule_family.py` | `derive_rule_family()` — derives a rule family from a linter rule name (ruff-b904 → ruff-b) for envelope tagging + per-family SPRT calibration. |
| `shared_pattern_library.py` | Thread-safe cross-repo pattern cache with confidence merging. |
| `composite_pattern.py` | Multi-step fixes that modify multiple files atomically. |
| `reforge.py` | Identifies refactor-class findings and routes them to the refactor queue. See [RULES_REFERENCE.md](../reference/RULES_REFERENCE.md) for routing precedence and context rules. |
| `refactor_queue.py` | Durable queue for human-review-gated refactors. |
| `regression.py` | Regression detection: finds test deletions, export changes, lint regressions. |
| `rebase_sweep.py` | Post-merge rebase: rebases sibling PRs after a merge. |
| `rebase_stats.py` | Structured telemetry for auto-rebase operations. |
| `clean_prs.py` | Stale PR detection and cleanup. |
| `cost_tracker.py` | Tracks model API invocation costs per cycle with soft/hard limits. |
| `escalation.py` | Pattern detection and threshold-based escalation. |
| `governance.py` | Event-sourced governance substrate: AssetRef, ApprovalRecord, Governance State projection (most-recent-wins over approval_records.jsonl trail), policy resolution (per-asset-class per-transition mode + safe-mode ceiling), SPRT reset boundary finder. No-op with empty trail. |
| `bundle_loader.py` | Golden Validation Bundle loader: reads product fixtures (`golden_bundles/`) + repo-state bundles with product-first precedence. `has_bundle_reference` for the promotion gate. |
| `synthesizer.py` | LLM synthesizer (build-time): generates canonical before/after/negative code examples per rule topic. Injectable `llm_callback`. (ADR-0018) |
| `validator.py` | Linter oracle (build-time): runs ruff/eslint on candidate before (must trigger) + after (must be clean). Auto-captures diagnostics + has_autofix. (ADR-0018) |
| `pipeline.py` | Synthesize-then-validate driver (build-time): orchestrates topic → synthesize → validate → package. Includes Python + JS topic lists. (ADR-0018) |
| `dry_replay.py` | Non-mutating evidence collection for matched-but-not-selected Patterns. File-level checkpoint/restore around the cascade's fix application. Records would-have-applied outcomes to `dry_replay.jsonl`. |
| `sprt.py` | Two-sided Sequential Probability Ratio Test (Wald): computes LLR from Dry Replay outcomes since most recent reset boundary. Demote when LLR ≤ B (broken); auto-promote when LLR ≥ A (healthy). LLR recomputed from durable stores — no accumulator. Per-family `p_healthy`/`p_broken` override from `calibration.yaml` (alpha.3); degenerate families fall back to ADR-0012 defaults. |

> **Canonical primitives** (all JSONL/JSON file I/O routes through these):
> - `state_io.py` — `atomic_json_write` + `rotate_jsonl_if_needed` (extracted per rec-08; closes engine→app layering for atomic writes)
> - `jsonl.py` — `read_jsonl` + `append_jsonl` (consolidates 50+ inline reimplementations)
> - `state.py:append_log` — `[ISO] message\n` structured logging primitive (`_append_text` is alias)
> - `models.py:IssueStatus` enum — locks 11 `needs-human-*`/`resolved-*`/`blocked_*` strings into typed contract (M7 `.value` pattern)
>
> **Layering enforcement**: `scripts/tools/enforce_architecture.py` runs as a CI gate and reports ZERO engine→app violations across `bluei/engine/` (1 accepted: `mnemo_client.py` — external Mnemo CLI dependency).
>
> **StateManager delegation** (Rec 07): `StateManager.{load,save}_state` / `{load,save}_issues` / `load_findings` / `append_findings` delegate to canonical engine owners. Engine owns schema; StateManager keeps path resolution.

### 4. Plugins (`plugins/`)

Eight language packs that detect issues via external tools:

| Plugin | Tool | What it finds |
|--------|------|---------------|
| `python/` | ruff, pylint, mypy | Lint errors, type issues, style violations |
| `typescript/` | xo, eslint, tsc | Code quality, type safety, complexity |
| `go/` | staticcheck | Bugs, style, dead code |
| `rust/` | clippy | Lint, style, performance |
| `shell/` | shellcheck | Quoting, injection, deprecated syntax |
| `dockerfile/` | hadolint | Unversioned images, apt cleanup, layer issues |
| `markdown/` | markdownlint | Header spacing, bare URLs, hard tabs |
| `test/` | - | Minimal plugin for unit testing the plugin system |

Each plugin has `plugin.py` (detection logic), `plugin.yaml` (manifest), and
`health_mapping.yaml` (maps linter rules to health signals).

---

## Core Concepts

### Finding

A detected issue. Fields include:

- `finding_id`, `repo`, `path`, `line` — location
- `rule` — the detection rule (e.g., `ruff-b904`, `type-explicit-any`)
- `snippet`, `confidence` — evidence and confidence score
- `fix_attempts`, `last_fix_error`, `fix_success` — failure memory (see Directive Seeding)
- `refactor_class`, `refactor_phase` — for refactoring classification

### Cycle

A run phase. Types:

- **issue-cycle** — discovery only. Finds issues and creates GitHub issues.
- **pr-cycle** — fix + PR. Fixes findings and opens pull requests.
- **merge-cycle** — auto-merge. Evaluates and merges ready PRs.
- **orchestrated** — full pipeline: issue → pr → merge.
- **review-cycle** — reviews existing PRs for actionable feedback, remediation churn.

### Care Level

Defines how autonomous bluei can be:

| Level | Issues | PRs | Auto-merge |
|-------|--------|-----|------------|
| `watch-only` | Dry-run only | No | No |
| `note-only` | Yes | No | No |
| `offer-fixes` | Yes | Yes | No |
| `full-care` | Yes | Yes | Yes |

### Pattern

A learned fix. Generated from successful diffs via `pattern_extractor.py`. Stored
in `fix_patterns.jsonl` with confidence scores. Replayed deterministically on
future matches via `pattern_replay.py`.

Patterns get:

- **Confidence scoring** — success/failure tracking
- **Structural hashing** — invariances for cross-repo sharing
- **Recipe promotion** — high-confidence patterns auto-promoted to YAML recipes
- **Composite patterns** — multi-file, multi-step fixes

#### Pattern Confidence Management

`pattern_confidence.py` manages fix pattern confidence based on feedback and replay
outcomes:

| Event | Delta | Description |
|-------|:-----:|-------------|
| Positive feedback | +0.10 | Confidence boost on accepted PR |
| Negative feedback | -0.20 | Confidence decay on rejected PR |
| Replay failure | -0.15 | Decay when replay doesn't fix the finding |
| Staleness (60 days) | → 0 | Patterns not verified within `STALENESS_DAYS` are deactivated |
| Below deactivation threshold | → 0 | Patterns falling below `DEACTIVATION_THRESHOLD` (0.3) are zeroed |
| Above auto-replay threshold | — | Patterns above `AUTO_REPLAY_THRESHOLD` (0.9) skip LLM confirmation |

### Campaign

A multi-phase batch of related fixes. Campaigns group findings by rule or path,
order them by dependency, and execute phases sequentially. Managed by:

- `campaign_planner.py` — strategy and ordering
- `campaign_executor.py` — dry-run and live execution
- `campaign_state.py` — persistent campaign plans

### Emergent Rule

A new detection rule proposed from repeated finding patterns. The lifecycle:

1. **Propose** — observe repeated findings, create candidate rules
2. **Validate** — promote safe proposals to candidates
3. **Shadow scan** — run candidates against worktrees, measure false positives
4. **Approve / Reject** — manual or automated gating
5. **Promote** — retire emergent rule and write as hand-authored plugin

---

## Advanced Capabilities

### Directive Seeding (Feedback Loop)

Directive seeding closes the loop between fix attempts and future fix prompts.
Without it, bluei would retry the same failing fix indefinitely without remembering
past attempts.

**What it does:**

1. **Finding-level failure memory** — each `Finding` tracks `fix_attempts`,
   `last_fix_error`, `fix_success`, and `last_fix_at`. Persisted in findings.jsonl.

2. **LESSONS_LOG integration** — `append_lesson()` in `utils.py` tags entries with
   `finding_id`. `load_lessons_for_finding()` and `load_lessons_for_rule()` parse
   the log to retrieve prior context.

3. **Exponential backoff cooldown** — `get_effective_cooldown()` in `state.py`
   implements `base * 2^N` backoff. A finding that has failed 3 times waits 8x
   longer than a fresh one. Capped at 7 days.

4. **Context injection** — before each fix attempt, `apply_claude_fix()` in
   `lifecycle.py` loads:
   - `fix_history` — prior attempts for this specific finding
   - `finding_record` — current state from findings.jsonl
   - `rule_history` — similar fixes for this rule in the codebase
   - `failure_clusters` — patterns of failures for this rule

   These are injected into the LLM prompt via `render_claude_fix_prompt()` in
   `prompts.py` as "Prior context", "Fix history", "Similar fixes", and
   "Failure patterns" sections.

**Design docs:** `docs/plans/DIRECTIVE_SEEDING_DESIGN.md` and
`docs/plans/DIRECTIVE_SEEDING_IMPL.md`.

### Deterministic Fix Cascade

Before falling back to LLM, bluei tries a sequence of deterministic fix stages:

1. **Linter auto-fix** — ruff-fix, ruff-format, pyupgrade, autoflake, isort
2. **Recipe engine** — declarative YAML recipes from built-ins, plugins, or repo-local
3. **Pattern replay** — matched stored fix patterns with structural hashing
4. **Composite patterns** — multi-step fixes with transaction semantics
5. **AST transforms** — tree-based code transformations

Only if all deterministic stages fail does bluei fall back to LLM (Claude or OpenCode).
Each stage produces structured telemetry for cost tracking and cascade optimization.

### Campaign Orchestration

Campaigns group related findings into ordered phases for batch execution:

- **Planning** — `campaign plan` groups findings by rule, path, or dependency graph
- **Execution** — `campaign run` walks phases sequentially, stopping on failure
- **Lifecycle** — pause/resume/abort with event history and reason tracking
- **Safety** — dry-run required for live execution, worktree isolation

**Dependency-aware ordering:** `import_graph.py` builds a directed import graph
from project source files (Python and TypeScript, regex-based) and provides a
topological sort so campaigns can target leaf modules first. This ensures dependent
files are fixed after their dependencies.

### Emergent Rule Discovery

bluei can propose new detection rules from observed patterns:

- Monitors repeated findings across runs
- Creates candidate rules with detection patterns and file globs
- Shadow-runs candidates against worktrees to measure false positive rates
- Promotes approved rules to hand-authored plugins

**Lifecycle state machine:**

```
PROPOSED → CANDIDATE → TENTATIVE → ACTIVE → REJECTED
                                 ↘ RETIRED
```

- **PROPOSED** — Rule created from repeated finding patterns or successful fix
  patterns. Requires `--min-observations` threshold (default 5).
- **CANDIDATE** — Passed validation: safe, not overlapping with built-in rules.
  Enters shadow mode for evidence collection.
- **TENTATIVE** — Shadow-scanned against worktrees. Must demonstrate low false
  positive rate over enough clean runs before activation.
- **ACTIVE** — Can create real findings via the explicit `emergent discover`
  command. Never auto-activates — always requires operator approval.
- **REJECTED** — Operator-rejected with reason. Preserved for audit.
- **RETIRED** — Was active but has been decommissioned (e.g., replaced by
  hand-authored plugin rule).

Only `ACTIVE` emergent rules can affect issue creation, campaigns, or health
reporting. `CANDIDATE` and `TENTATIVE` rules are shadow-only.

### Health Scoring

A composite 0-100 score derived from finding counts, severity, and coverage data.
Health scores are tracked over time in `health_history.jsonl` for trend analysis.
The broader system (runner.py, review.py) feeds aggregate findings into the
health engine.

### Cycle Signals

`cycle_signals.py` provides a cross-cycle awareness bridge so the issue, PR, review,
and merge cycles can share findings about what's working and what's not.

- **Suppression mechanics:** The review cycle detects rules that keep failing retries.
  After `RETRY_FAILURE_SUPPRESSION_THRESHOLD` (4) consecutive retry failures, the rule
  is suppressed for `SUPPRESSION_DURATION_CYCLES` (24 cycles, ~12h at 30min intervals).
- **Cross-cycle flow:** Issue cycle checks suppressed rules before creating new issues.
  PR cycle skips suppressed rules. Review cycle records failures into
  `review_stats.jsonl`, which `record_retry_failure_pattern()` reads to detect patterns.
- **Expiration:** Suppressed rules expire automatically — suppression entries carry
  an `expires_at` timestamp and are pruned on read.
- **Explicit lift:** Operators can call `lift_suppression()` to manually clear a rule.
- **State file:** `CycleSignalStore` persists to a shared JSON file per repo, parsed
  by all cycles.

See also: [GITHUB_OPERATIONS.md](GITHUB_OPERATIONS.md) for the review telemetry format.

### Auto-Tuning

`auto_tune.py` adapts detection thresholds and batch sizes based on telemetry from
`review_stats.jsonl`:

**Tuning modes:**

| Mode | Trigger | Action |
|------|---------|--------|
| Batch reduction | ≥4 consecutive `retry_failed > 0` records | `max_prs_per_run` halved (min 1) |
| Cooldown extension | ≥4 consecutive `findings_failed > 0` records | `finding_cooldown_seconds` doubled (max 12h) |
| Replay threshold adjustment | Per-rule success/failure tracking | Threshold raised on failures, lowered on 20 consecutive successes |
| Recovery | Tuned cycle succeeds | Gradual step-back: batch size incremented, cooldown halved, reset at baseline |

**Parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CONSECUTIVE_RETRY_FAILURE_THRESHOLD` | 4 | Consecutive retry failures before tuning |
| `COOLDOWN_BOOST_MULTIPLIER` | 2.0 | Multiplier for cooldown on failure pattern |
| `BATCH_REDUCTION_FACTOR` | 0.5 | Factor to reduce batch size |
| `MIN_BATCH_SIZE` | 1 | Floor for batch reduction |
| `MAX_COOLDOWN_HOURS` | 12 | Ceiling for cooldown extension |
| `REPLAY_THRESHOLD_FLOOR` | 0.7 | Minimum replay threshold |
| `REPLAY_THRESHOLD_CEILING` | 0.98 | Maximum replay threshold |
| `REPLAY_SUCCESS_WINDOW` | 20 | Consecutive successes to lower threshold |
| `REPLAY_FAILURE_WINDOW` | 10 | Window for counting failures |

**Feedback loop:** Review cycle → `review_stats.jsonl` → `compute_tune()` reads last 20
records → writes `tune_state.json` → runner reads `read_tune_overrides()` → applies
CLI arg overrides → next cycle runs with adjusted params. `flag_tune_success()` and
`reset_tune()` handle recovery.

### Self-Healing

`healer.py` runs on every startup:

- Cleans stale lock files (>4h old)
- Prunes orphaned worktrees
- Repairs corrupted state files (6 known corruption patterns)
- GC's registry entries for missing repos
- Cleans stale batch records

### Escalation

The escalation system monitors four conditions and writes structured JSONL
records to `escalation_log.jsonl`:

| Condition | Trigger | Severity | Response |
|-----------|---------|----------|----------|
| Cycle failure | Any cycle exits with error | error | Logged, may block next cycle |
| Merge failure | `merge_pr()` fails | warning | PR triaged back to pr-cycle |
| Dedup saturation | ≥3 open PRs for same finding (configurable via `--max-duplicate-prs-threshold`) | warning | Excess PRs closed, newest kept, finding routed to human review, automated cycling paused |
| Rebase conflict trend | Increasing conflict rate over successive sweeps | info | Flagged for operator review |

The `write_escalation()` function in `bluei/app/escalation.py` and the separate
`escalation.py` in `bluei/engine/` write to a unified log. The
dedup response (`handle_max_duplicate_escalation()`) auto-closes excess PRs
and routes to `needs-human-max-duplicates-exceeded` status.

### Architecture Enforcement

`scripts/tools/enforce_architecture.py` provides AST-based module boundary enforcement. It checks that imports between packages follow the declared architecture rules — for example, preventing execution engine modules from importing `bluei.app.*` (rec-08 layering violations). The script runs as a CI gate; it is NOT a runtime module and is not importable as `bluei.engine.enforce_architecture`. Violations are logged and fail the CI build.

### Completeness Checking

`scripts/tools/check_completeness.py` validates that every finding in the system has a fix attempt. It is a standalone audit script run ad-hoc or as a CI gate; it is NOT a runtime module and is not importable as `bluei.engine.check_completeness`. The script scans the findings log and verifies that each finding is either resolved, has a pending fix attempt, or is explicitly marked as needing human review. Orphaned findings (no fix attempt and not human-gated) are reported.

---

## Data Flow

### Scan Flow

```
  bluei scan <repo>
       ↓
  orchestrator.discover_findings()
       ↓
  linters.py: ruff, xo, staticcheck, etc. (per language plugin)
       ↓
  findings written to findings.jsonl
       ↓
  escalate, auto-tune thresholds
       ↓
  create GitHub issues for new findings
```

### Fix Flow

```
  bluei clean <repo>
       ↓
  state.load_findings() → filter by cooldown
       ↓
  lifecycle.apply_autofix(finding)
       ↓
  ┌─ cascade: try deterministic (recipe → pattern → AST → composite) ─┐
  │  └─ first success wins, return result                              │
  └─ fallback: apply_claude_fix()                                     ┘
       ↓ (directive seeding)
  load lessons, finding_history, rule_history, failure_clusters
       ↓
  render_claude_fix_prompt() with context injection
       ↓
  LLM generates fix → apply diff → run_validation_gate()
       ↓
  on success: append_lesson(), update finding, create/update PR
  on failure: mark finding, apply exponential backoff cooldown
```

### Merge Flow

```
  bluei run <repo> --phase merge-cycle
       ↓
  gh.fetch_open_prs_for_merge() → deterministic oldest-first ordering
       ↓
  for each PR: evaluate_pr_mergeability()
       ↓
  regression.check_regressions() → block if score > 0.5
       ↓
  merge one PR per run, triage DIRTY/BEHIND back to pr-cycle
```

---

## State and Persistence

All state is stored under `$QA_AGENT_WORKSPACE/repos/<repo>/state/`:

| File | Content |
|------|---------|
| `findings.jsonl` | All discovered findings (JSONL) |
| `issues.json` | Open/closed GitHub issues |
| `active_prs.json` | Open PRs with status |
| `fix_patterns.jsonl` | Learned fix patterns |
| `health_history.jsonl` | Health scores over time |
| `escalation_log.jsonl` | Escalation events |
| `cost_log.jsonl` | Model API costs |
| `rebase_stats.jsonl` | Rebase telemetry |
| `review_state.json` | PR review lifecycle state |
| `campaigns/` | Saved campaign plans |

Each state file follows a specific JSONL/JSON schema. Key record types:

- **findings.jsonl** — `Finding` records: `finding_id`, `repo`, `path`, `line`, `rule`, `snippet`, `confidence`, `fix_attempts`, `last_fix_error`, `fix_success`, `refactor_class`, `refactor_phase`
- **health_history.jsonl** — Health snapshots: component scores, overall score, timestamp
- **escalation_log.jsonl** — Escalation events: `timestamp`, `findings[].type` (info/warning/error), `findings[].message`
- **cost_log.jsonl** — Cost records: `model`, `input_tokens`, `output_tokens`, `estimated_cost`, `timestamp`
- **fix_patterns.jsonl** — Pattern records: `pattern_id`, `finding_id`, `before`, `after`, `rule`, `confidence`, `success_count`, `hash`

---

## Configuration

Repos are configured via `registry.yaml`:

```yaml
repos:
  - name: my-project
    path: /path/to/repo
    language: python
    mode: watch-only    # care level
    profile: conservative
    enabled: true
version: '1.0'
```

`templates/config.yaml.template` in the repo root provides the full config schema
with all available keys. See also `docs/CONFIG_REFERENCE.md`.

---

## Testing

~5,821 tests across 200+ files (2026-06-18). All use `tmp_path` fixtures — no external services
needed. Run with:

```bash
./scripts/test-all.sh
# or
PYTHONPATH=. python -m pytest tests/ -q
```

---

*This document covers the system as of version 2.6.0.*
