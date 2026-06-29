# Changelog

All notable changes to `bluei` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [0.2.0-alpha.4] — Deterministic Assets

### Structural Replay (ADR-0019)

- **AST-aware Pattern application.** When a Pattern's canonical `before_snippet` doesn't literally match the target file (different variable names, imports, layout), Structural Replay parses both into ASTs, structurally matches with a variable-binding map, derives the AST edit from the before→after delta, and applies the same edit to the matched target subtree. Activates as a fallback inside `PatternReplayCascadeStage` on literal miss — same HIT/MISS/FAILURE evidence path, same SPRT/Dry Replay consumers.
- **Two-stage confidence gate:** Pattern confidence ≥ AUTO_REPLAY (0.9) AND `fuzzy_structural_match` ≥ rule-category threshold AND binding-map completeness = 1.0 (every pattern variable resolved to a target counterpart).
- Python-only in alpha.4; TypeScript Structural Replay deferred.

### Recipe Foundry (ADR-0020)

- **Build-time LLM-authoring pipeline** (`bluei/tools/foundry/`). Consumes seeded Patterns, invokes an LLM via injectable callback to author full Recipe YAML, validates each proposal (strict `parse_recipe` schema + detector oracle), and writes to `recipes/staged/` under policy routing.
- **Policy-driven approval:** Default `gate_open_audit` (build-time) = auto-write + `auto_promote` record. Explicit `gate_closed` = cache YAML in ApprovalRecord's `evidence_snapshot` + `pending_approval` → surfaces in `bluei learn inbox` → `learn approve` → `resume_pending_promotion` writes the YAML.
- **`parse_recipe` refactor:** Split from `load_recipe(path)` so the Foundry can parse LLM-emitted YAML strings without tempfile IO.
- **`resume_pending_promotion`:** Reads cached YAML from `evidence_snapshot`, writes to staged dir on approval.
- Foundry first-run seeded-Recipes pending (T2.5 operational step — needs `claude_author_cmd_template` config wiring + LLM run against 38 seeded Patterns).

### Rule Hatchery: Detection Extraction + Evidence Hardening (ADR-0021)

- **Regex-based detection extraction.** Replaced the stub mechanism (`search_pattern = linter rule name`) with `extract_detection_regex(snippet, language)`: tokenizes identifiers via AST walk, replaces each with `\w+` capture groups. `propose_rules_from_findings`/`propose_rules_from_fix_patterns` now produce REGEX_PATTERN rules from actual code snippets.
- **`scan_shadow_rules` regex branch.** `DetectionType.REGEX_PATTERN` dispatches to `re.search` with compiled-regex caching alongside the existing TEXT_PATTERN substring path.
- **Rule-owned FP measurement.** New `EmergentRule.negative_examples: List[str]` field. `measure_false_positives(rule)` runs the rule's regex against its own negatives. The existing FP-rate gate (`record_shadow_run` auto-promote at ≤ 0.2) is now fed real data instead of the caller passing zero.
- **Bundle gate dropped.** No bundle links to emergent rules (`asset_ref: null` on all shipped bundles); the two-tier gate was vacuous. FP-rate gate via rule-owned negatives is the sole ACTIVE gate.

### Seeded Patterns Substrate (Slice 0)

- **38 seeded Patterns** populated across 12 rule families (30 Python/ruff + 7 JS/eslint + 1 other) — alpha.3 shipped Bundles but not the parallel Patterns. Deterministic regenerator reads existing bundles, re-validates via linter, produces Patterns without LLM.
- **Broadened packaging gate:** `if parsed.after:` (was `if not has_autofix and after:`). All rules with a fix shape produce BOTH a Bundle and a seeded Pattern — defense-in-depth (Recipe cascade fires first; Pattern is fallback).

### Governance Decision Verbs (ADR-0015 revised)

- **`bluei learn {approve, reject, pause, resume, retire}`** — five cross-asset governance decision verbs that append ApprovalRecords. `learn approve` additionally calls `resume_pending_promotion` for `recipe:` asset_refs. The `learn` namespace now hosts both reads (inbox/status/audit/bundle) and cross-asset governance decisions, per ADR-0015's three-category write split.

### eslint Autofix Wildcard

- **`recipes/built-in/eslint-autofix.yaml`** — command-handler wildcard parallel to `ruff-autofix.yaml`, covering autofixable `eslint-*` rules. RecipeEngine precedence (exact +50 > prefix +30) ensures specific detection-only Recipes win over wildcards.

### ADRs

- **ADR-0019:** Structural Replay lives in the Pattern replay stage with a two-stage confidence gate.
- **ADR-0020:** Recipe Foundry is a build-time LLM-author pipeline; governance approval is policy-driven (refines ADR-0007 runtime/build-time axis).
- **ADR-0021:** Emergent Rule detection uses regex abstraction now, AST STRUCTURAL in alpha.5.
- **ADR-0007 refined:** Distinguishes runtime silent promoters (gate-closed) from build-time curated pipelines (gate-open; human commit IS the approval).
- **ADR-0015 revised:** Three write categories — governance decisions (cross-asset, under `learn`), native lifecycle transitions (stay native), asset-content edits (stay native).

### Testing

- 6,360 tests across 170+ test files. 9 skipped. +65 new tests from alpha.3 baseline (6,295).
- Run via `PYTHONPATH=. .venv/bin/python -m pytest tests/ -q`.

---

## [0.2.0-alpha.3] — Evidence Foundation

Populates the empty alpha.2 substrate with provenance-aware infrastructure, authoritative guidelines, envelope field completion, and a synthesize-then-validate Seed Library pipeline (ADR-0018) shipping 38 validated canonical bundles across Python (ruff) and JavaScript (eslint).

Still pre-release software. Do not use in production.

### Provenance-Aware Mining Infrastructure

- **`open_pattern_store()` factory** — canonical production construction path. Loads product-fixture seeded patterns into the store's in-memory index after construction. Migrated 4 runtime construction sites (cli, emergent, feedback, cascade lookup).
- **`register_asset()` producer interface** — centralized runtime insertion path (dedup + source stamp). Pattern extraction routes through it; establishes the interface for alpha.4's Recipe Foundry and Rule Hatchery.
- **Provenance by storage locus** — product dirs = `authoritative-seed` (ACTIVE bypass); repo-local = `mined` (governance gate). Open vocabulary, additive.

### Envelope Field Completion

- **`imports_touched`** — new import-name visitor (`extract_imports_touched`) preserves real module names (Python + TS/JS). Wired into `pattern_extractor.extract()` and the packaging layer.
- **`validation_commands_passed`** — `_run_baseline_validation` returns `Tuple[bool, List[str]]`; Dry Replay records now carry which checks passed instead of hardcoded `[]`.
- **`rule_family` taxonomy** — `derive_rule_family` helper (ruff-b904 → `ruff-b`; eslint-no-unused-vars → `eslint-no-unused`). Preserves ruff's B/E/F/S/C4 groups as distinct families for per-family calibration.
- **GoldenBundle enrichment** — `imports_touched` + `negative_examples` additive fields.

### Authoritative Guidelines (ADR-0017)

- **Prompt-context guidelines** — PEP 8 naming, OWASP input validation, TypeScript conventions shipped as YAML files under `bluei/engine/authoritative_guidelines/`. Loaded by rule family + language into LLM fix prompts via Directive Seeding. NOT governed assets.
- **`guideline_loader.py`** — runtime loader wired into `render_claude_fix_prompt` via `ClaudeFixRequest.authoritative_guidelines`. Appears in both pr-cycle and refactor-queue LLM prompts.

### SPRT Per-Family Calibration

- **Synthetic calibration harness** — `variations.py` generates structurally-diverse code variations per rule family; `calibrate.py` measures structural-match rates and derives per-family `p_healthy`/`p_broken`. Degenerate distributions fall back to ADR-0012's hand-picked defaults.
- **`_sprt_params` per-family override** — reads `calibration.yaml` inline (no engine→tools import) and overrides `p_healthy`/`p_broken` for non-degenerate families.

### Seed Library — Synthesize-then-Validate (ADR-0018)

- **`ParsedRule` + `package.py`** — source-agnostic packaging layer. Converts structured rule tuples into Golden Bundles + seeded Patterns with provenance `authoritative-seed`. Language-agnostic; accepts tuples from any source.
- **`seeded_pattern_loader.py`** — runtime loader for product-fixture seeded patterns. Called by `open_pattern_store()` factory.
- **`synthesizer.py`** — LLM synthesizer. Takes a Topic (rule code, description, language), generates a canonical before/after/negative candidate via an injectable `llm_callback`. Output is original bluei-owned content.
- **`validator.py`** — linter oracle. Runs ruff/eslint on the candidate's before (must trigger the rule) and after (must be clean). The linter is the oracle, not the source. Auto-captures diagnostic messages and auto-detects `has_autofix` from the linter's fix field.
- **`pipeline.py`** — driver with topic lists for Python (20 ruff topics) and JavaScript (10 eslint topics). Orchestrates synthesize → validate → package.
- **38 validated canonical bundles shipped** — 30 Python (ruff-validated) + 7 JavaScript (eslint-validated) + 1 hand-authored original. All synthesized as original content, verified by the linter oracle (before triggers, after is clean). 5/43 candidates rejected by the oracle — the quality gate working as designed.

2 new ADRs (0017, 0018). Test suite: 6245 → 6295 (+50 tests, all additive).

---

## [0.2.0-alpha.2] — Safe Learning

The safety substrate for learned automation. Governance, evidence collection, fix validation, and statistical demotion — all shipping inert (proven by synthetic tests, unexercised on real data until alpha.3 self-seeding populates the evidence base).

Still pre-release software. Do not use in production.

### Operator Control Plane

- **Event-sourced governance substrate** — `ApprovalRecord` trail (`approval_records.jsonl`) is the single source of truth; Governance State (ACTIVE/PAUSED/RETIRED) is a read-time projection (most-recent-decision-wins). No separate state file. Crash-safe, auditable, no synchronization issues. (ADR-0006, ADR-0008)
- **Per-class policy-driven promotion gate** — `promote_pattern_to_recipe` re-routed through approval gate. Write-producing promotions are gate-closed by default (queue ApprovalRecord, no YAML written); in-place mutations are gate-open-with-audit. Policy file schema with per-asset-class per-transition mode field. (ADR-0007)
- **Cascade-stage consultation layer** — all 4 cascade stages (Pattern, Recipe, AST Transform, Emergent Rule) consult Governance State via per-candidate `is_governance_active` checks. PAUSED/RETIRED assets are skipped. Per-candidate fallthrough for Pattern stage (PAUSED exact match falls through to structural/fuzzy lookup). (ADR-0008)
- **Safe-mode tri-state flag** — `learning.mode` config key (`active` / `audit_only` / `paused`). Ceilings over per-class policy. Paused freezes everything; audit_only forces gate-closed and disables SPRT firing. (ADR-0013)
- **`bluei learn` CLI namespace** — unified read/inbox surface: `learn inbox` (pending approvals), `learn status` (governance + native + SPRT evidence), `learn audit` (full decision history), `learn bundle` (create Golden Validation Bundle from known-good fix). Write-verbs stay native. (ADR-0015)

### Golden Validation Bundles

- **Bundle container + storage** — hybrid filesystem-boundaried: product fixtures (`bluei/engine/golden_bundles/`) ship curated and public; repo state (`repos/<name>/state/golden_bundles/`) is operator-private. Privacy enforced by filesystem layout (bundle validation requires original code, can't be privacy-normalized). (ADR-0009)
- **Minimal forward-compatible schema** — `before`, `after`, `detector_before`, `detector_after`, `validation_command`, metadata. Additive fields (`negative_examples`, `imports_touched`, `validation_commands_passed`, `rule_family` populated) land in alpha.3 without breaking alpha.2 bundles. (ADR-0016)
- **ruff-b904 proof fixture** — first product fixture shipped, validating the substrate end-to-end.
- **Bundle-gated promotion** — Patterns with zero bundle references cannot be promoted to Recipes under gate-closed policy.

### Dry Replay

- **Non-mutating evidence collection** — for matched-but-not-selected Patterns (lookup matched but cascade winner was a different stage), Dry Replay records what the Pattern *would have* done without affecting the live worktree. File-level checkpoint before fix flow → restore original → apply Pattern → validate → record outcome → restore winner. (ADR-0011)
- **Per-cycle cap** — 20 Dry Replay attempts per cycle, oldest-first prioritization. Cap limits attempts (outcome unknown until after expensive validation).
- **`dry_replay.jsonl` sink** — append-only, carries `run_id`, `pattern_id`, `finding_id`, `would_have_outcome` (HIT/MISS/FAILURE). MISS outcomes don't contribute to SPRT.

### SPRT Demotion

- **Two-sided Sequential Probability Ratio Test** — Wald's SPRT replaces hardcoded demotion thresholds. Operator-facing inputs are risk tolerances (α, β, p₀, p₁), not magic counts/rates. Demote when LLR ≥ A; auto-re-promote when LLR ≤ B. Hysteresis is structural (band between boundaries). (ADR-0012)
- **LLR recomputed from durable stores** — `dry_replay.jsonl` outcomes since most recent reset boundary (from `approval_records.jsonl`). No stored accumulator; crash-safe.
- **FAIL is 2.7× more impactful than HIT** — `ln((1-p₁)/(1-p₀))` / `ln(p₁/p₀)` at defaults. No "negative-priority bypass" needed — SPRT weighting handles it.

### Plumbing

- **`run_id` correlation key** — UUID4 per CLI invocation, stamped on `cascade_resolutions.jsonl`, `cost_log.jsonl`, and `dry_replay.jsonl` rows. Enables cross-cycle joins for cumulative features. Additive — old rows lack `run_id`, no backfill. (ADR-0010)
- **Cascade-internal savings** — Pattern/composite replay wins inside the cascade now contribute real $ savings via `cost_tracker` threaded into `CascadeContext`. Previously counts-only ($ = "—").

### ADRs

- 0006: All 4 asset classes (Pattern, Recipe, AST transform, Emergent Rule) governed through unified substrate
- 0007: Policy-driven promotion, split default (write-producing gate-closed, in-place gate-open-audit)
- 0008: Governance State = projection of ApprovalRecord trail (event-sourcing)
- 0009: Hybrid bundle storage, filesystem-boundaried privacy
- 0010: run_id correlation key on ledger sinks
- 0011: Dry Replay scope (matched-but-not-selected, capped) + scratch mechanism (file-level checkpoint)
- 0012: Two-sided SPRT for demotion/re-promotion; PAUSED-only effect; LLR recomputed
- 0013: Safe-mode tri-state flag (active/audit_only/paused)
- 0014: SUPERSEDED (release scoping decision, not architectural)
- 0015: bluei learn namespace (read unified, write native)
- 0016: Bundle schema minimal and forward-compatible

### Testing

- 6,245 tests across 210+ files (+128 from alpha.1). All synthetic — no live cycles run during alpha.2 by design.
- New test files: `test_run_id_plumbing.py`, `test_bundle_loader.py`, `test_governance.py`, `test_cascade_governance.py`, `test_promotion_gate.py`, `test_safe_mode.py`, `test_dry_replay.py`, `test_sprt.py`, `test_learn_cli.py`.

---

## [0.2.0-alpha.1] — Evidence

Flywheel Ledger (deterministic-vs-LLM resolution metrics, cascade-stage counts, honest three-way HIT/MISS/FAILURE replay outcomes, standalone-Pattern-replay $ savings) + Pattern Court Lite (`last_failed_at`/`excluded_paths` envelopes with lookup-layer enforcement, enriched `patterns show`/`list`, `exclude`/`unexclude` producer). Rendered across the `clean` summary, `report` (markdown + HTML), and dashboard. Observability only — no fix-behavior changes.

---

## [0.1.0-beta.1] — Post-alpha hardening

Alpha to beta transition for the 0.1.0 line. Architecture consolidation,
god-module decompositions, and merge-cycle audit closure on top of the
initial pre-release. No new user-facing features; this release is internal
quality work that makes the deterministic fix and review pipelines
maintainable. The deterministic flywheel direction begins in 0.2.0-alpha.1.

Still pre-release software. Do not use in production.

### Architecture

- **Tier 1 architecture cleanup** — ALL items complete: `cycle.py` decomposition (C3), `cli.py` decomposition (C4), `lifecycle` singletons (H4), `orchestrator` 4-module split (S3), `structural_hash` package (S4), `pipeline` phase extraction (S5), `batch_recovery` extraction (S2), observation + autonomous cycle decomposition (rec-16, rec-18).
- **God-module decompositions** — ALL complete: `engine/batch_pr.py` split into `batch_execution` / `batch_grouping` / `batch_pr_creation` / `batch_recovery`; `engine/gh.py` (1222L) converted to the `engine/gh/` package (8 submodules); `review/observation.py` (1150L) split into 5 focused mixins; `review/autonomous.py` mixin-aligned.
- **H5 review→app decoupling** — COMPLETE. `bluei/common/` layer extracted for shared types (`Repo`, `RepoConfig`, `ReviewMode`, `ReviewRun`, `FeedbackEvent`). `review/` no longer imports from `app/`. Zero review→app violations.
- **Engine→app layering** — 4 of 5 violation classes closed. Zero `engine → app` imports in `bluei/engine/` (1 accepted: `mnemo_client.py`, an external dependency). `enforce_architecture.py` runs as a CI gate.

### Behavior fixes

- **Merge-cycle audit closure** — all 21 items from `MERGE_CYCLE_AUDIT.md` fixed: DISMISSED review gate, observation state recovery, review-state race locking, merge-queue detection, HAS_HOOKS handling, worktree reuse validation, loop-count semantic guard, `retry_pending_push` escalation, configurable PR limits, language-aware candidate scanning, inter-PR rate throttling, atomic-write fsync, JSONL rotation, datetime cooldown comparison, `generate_id` entropy, runner finally-block protection.
- **F1 rule_pack wiring** — `select_rule_pack()` wired into onboarding: interactive `_prompt_rule_pack_confirmation` + `--rule-pack` CLI override.
- **Pattern learning fixes** — confidence preservation on insert, `_check_staleness` fallback to `created_at`, `_matches_file_pattern` globstar support.
- **Notifications hardening** — digest severity filter, token masking, dashboard test split.

### Internal

- **State schema reconciliation (Rec 07)** — `StateManager` delegates all shared schemas to canonical engine owners. Engine owns schema; `StateManager` is a path resolver.
- **JSONL primitive consolidation** — `engine/jsonl.py` (`read_jsonl` + `append_jsonl`) replaces 50+ inline reimplementations. All inline JSONL sites migrated.
- **E2E test suite** — 83 tests across 9 domains. Suite now 6,017 tests across 200+ files, all offline and deterministic.

---

## [0.1.0-alpha.1] — Initial Pre-Release

First public pre-release of bluei — an autonomous QA agent for GitHub repositories.

### Core Engine

- **Discovery engine** — scans repos for specks (lint errors, type issues, test gaps, performance smells, doc drift) across 8 plugin languages (Python, TypeScript, Go, Rust, Shell, Markdown, Dockerfile, plus a test fixture plugin).
- **Deterministic fix cascade** — 10-stage pipeline: ruff-fix, ruff-format, pyupgrade, autoflake, isort, recipe engine, pattern replay (structural hashing + fuzzy matching), composite patterns, AST transforms, then LLM fallback.
- **Recipe engine** — staged.yaml and builtin.yaml recipe catalogs classify findings by rule, provide fix strategies with `SAFE`/`CAUTION`/`EXPERIMENTAL` safety labels, and apply deterministic fixes.
- **Tiered validation** — T0 (guaranteed) through T4 (structural) fix confidence tiers drive validation strictness. Auto-rollback on validation failure. Tier escalation for repeatedly-failing rules.
- **Pattern learning** — successful fixes are extracted as reusable `FixPattern` entries (structural hashing, fuzzy matching, cross-language patterns). `SharedPatternLibrary` and `CompositePattern` enable multi-step fix orchestration.
- **Batch PR system** — groups related findings into batched pull requests. Conflict detection prevents overlapping file edits. Failure-aware splitting with recursive retry and crash recovery.
- **PR automation** — `issue-cycle` creates GitHub issues from findings. `pr-cycle` applies fixes and opens PRs. `merge-cycle` handles PR merge/rebase. `rebase-sweep` rebases sibling PRs after merge.
- **Orchestration** — `pipeline.py` ties discovery → issues → fixes → PRs → merge into full sweep. Supports per-phase execution with locking.
- **GitHub integration** — `gh.py` wraps `gh` CLI with retry on transient failures (3 attempts, exponential backoff 1s/2s/4s). Handles issue creation, PR lifecycle, GraphQL live counts, self-merge repo list.

### Autonomous Review

- **Review cycle engine** — GitHub-native review lifecycle with observation, autonomous-review, and remediation modes. 51-method `ReviewCycleEngine` class in `review/cycle.py`.
- **Backend dispatch** — configurable review backend (local, Claude, custom template). Prompt rendering with context assembly, mnemo-augmented context, and candidate signal injection.
- **Publication pipeline** — candidate findings flow through normalization, identity assignment, deduplication, eligibility checks, chunking, and publishing with `PublishStatus` tracking.
- **Feedback learning** — provider-style feedback capture, normalization to `FeedbackEvent` dataclasses, pattern confidence adjustment from review feedback.
- **Safety monitoring** — `MonitoredSafetyState` tracks circuit-breaker status, failure counts, auto-rollback state. Guarded live-review with shadow mode, limited mode, and local-only fallback.
- **Chunking** — `ChunkManifest` supports `FULL_DIFF`, `COMPRESSED`, and `MULTI_PASS` compression modes for LLM context management.

### Application Layer

- **Onboarding** — `bluei init` (interactive wizard) and `bluei onboard` (headless batch). Detects language, framework, build tool, package manager. Generates per-project `config.yaml` from templates.
- **Health engine** — calculates 0-100 vitality score from findings across components. `HealthScore`, `Baseline`, emergent rule inference, category weighting.
- **State management** — `StateManager` persists findings, issues, review runs, feedback events to JSONL state files. Typed wrappers for `ReviewRun` and `FeedbackEvent` dataclasses. Per-repo state isolation.
- **Configuration** — `ConfigManager` handles repo configs, templates, and rule packs. `load_global_config()` bridges workspace-level settings (mnemo, github, report) to the engine without circular imports. Per-repo validation config via `config.yaml`.
- **Registry** — `RepoRegistry` tracks onboarded repos. `RepoConfig` with validation, safety modes, review settings.
- **Dashboard** — read-only observability dashboard generation.
- **Reporting** — `bluei report` generates JSON/HTML reports from state files (`engine/report.py`). PDF reports via `app/report.py`.

### CLI — 20+ Commands

```
bluei init              Register a project (interactive or --path)
bluei onboard           Headless onboarding
bluei scan <name>       Run discovery/issue-cycle scan
bluei clean <name>      Fix specks and open PRs
bluei duster <name>     Dry-run scan — preview without changes
bluei run <name>        Full orchestrated sweep
bluei status <name>     Show agent or project status
bluei health <name>     Show vitality score (0-100)
bluei report <name>     Generate vitality report (JSON/HTML/PDF)
bluei dashboard         Generate observability dashboard
bluei doctor <name>     Run operational diagnostics
bluei secrets <path>    Scan for hardcoded secrets
bluei languages         List installed language packs
bluei preflight <name>  Assess a project before registering
bluei heal <name>       Heal dirty worktrees
bluei ci <name>         Generate GitHub Actions CI workflow
bluei install-hook      Install pre-commit hook
bluei install-cron      Install host cron schedule
bluei patterns          Inspect learned fix patterns
bluei campaign          Plan multi-finding refactor campaigns
bluei emergent          Inspect proposed emergent rules
```

### Plugins & Language Support

- 8 language plugins: `plugins/go/`, `plugins/python/`, `plugins/rust/`, `plugins/typescript/`, `plugins/test/` (fixture), `plugins/shell/`, `plugins/markdown/`, `plugins/dockerfile/`.
- Plugin manifest system (`plugin.yaml`) — declares supported languages, detector rules, detection files, and tool configuration.
- Dual loading architecture: `app/plugins.py` (registry/manifests/health mappings) and `engine/plugin_loader.py` (runtime detection/loading/execution).

### Notifications

- Multi-channel notification framework: Slack (Block Kit), email (SMTP with TLS), webhook, and log file.
- `@register_notifier` decorator pattern for extensibility.
- Rate limiting per-finding (cooldown) and per-channel-URL (hourly cap).
- Env var resolution (`${VAR}`) in notification config with missing-var detection.

### Campaigns

- `CampaignPlanner` groups findings by rule for batch refactoring.
- `CampaignExecutor` manages phased execution (`planning` → `active` → `completed`/`aborted`).
- `CampaignStateManager` persists campaign state and events.

### Refactoring & AST

- **AST engine** — `ast_engine/` with `matcher.py`, `transformer.py`, `context.py`, `cascade.py`. Supports Python AST transforms (visit, replace, insert). TypeScript parser via `ts_parser.py`.
- **Refactor queue** — `refactor_queue.py` manages queued refactor work items with status tracking.
- **Reforge** — `reforge.py` classifies findings (`SIMPLE_FIX`, `REFACTOR_CLASS`, `CLAUDE_FIX`, `LARGE_FILE`). Size safety gates prevent auto-fix on files beyond limits.

### Mnemo Integration

- Cross-finding context recall via `mnemo_client.py` — pattern search, file relevance, call-graph analysis, dependency tracking.
- Recall-augmented fix prompts and review context assembly.
- Configurable limits via `config.yaml` (`mnemo.max_patterns`, `mnemo.max_chars`, etc.).

### Data Models

- **Unified `Finding` model** — single dataclass with 17 fields (`finding_id`, `repo`, `path`, `line`, `rule`, `snippet`, `confidence`, `safe_to_autofix`, `quick_win`, `category`, `language`, `refactor_class`, `refactor_phase`, `fix_attempts`, `last_fix_error`, `last_fix_at`, `fix_success`).
- **Enums** — `FixStatus`, `FixMethod`, `FixEngine`, `FixTier`, `BatchSeverity`, `BatchGroupBy`, `BatchStatus`, `ReviewMode`, `LiveRolloutMode`, `PublishStatus`, `FindingSource`, `FindingActionability`, `FindingSeverity`, `FeedbackSentiment`, `FeedbackSource`, `LearnedRuleStatus`, `ReviewRunStatus`.
- **Dataclasses** — `FixResult`, `BatchRule`, `BatchGroup`, `TieredValidationResult`, `ReviewCycleResult`, `PRSnapshot` (TypedDict), `ReviewComment` (TypedDict), `ReviewRecord` (TypedDict), `RemediationPlan` (TypedDict), `ReviewFinding`, `ReviewSummary`, `ReviewRun`, `FeedbackEvent`, `MonitoredSafetyState`, `Repo`, `RepoConfig`, `Run`.
- **Exception hierarchy** — `BlueiError` base + `GitHubAPIError`, `FixEngineError`, `WorktreeError`, `StateCorruptionError`, `ConfigurationError`.

### Testing

- 5,281 tests across 170+ test files. 9 skipped. Shared fixtures in `conftest.py` (`make_finding`, `git_repo`, `git_commit_all`).
- Run via `PYTHONPATH=. python -m pytest tests/ -q`. Bootstrap with `./scripts/bootstrap.sh`.

### Distribution

- **PyPI** — `pyproject.toml` with setuptools build, Python 3.11+, entry point `bluei = bluei.__main__:run`. Optional `[dev]` (pytest) and `[ast]` (tree-sitter) extras.
- **npm** — `package.json` for npm distribution (`npx bluei`). Wraps Python binary.
- **Install script** — `curl -fsSL https://bluei.dev/install.sh | bash`.

### License

- AGPL-3.0-or-later. Contributor CLA in `CLA.md`. See `OPEN_SOURCE_POLICY.md`, `TRADEMARK.md`, and `CONTRIBUTING.md` for full terms.
