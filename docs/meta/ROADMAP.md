<!-- CATEGORY: meta -->

# Roadmap — bluei

What's been built, and what's next.

---

## Shipped in v0.1.0-alpha.1

### Core Engine

- **Multi-stage deterministic fix cascade** — 6 linter stages (ruff-fix, ruff-format, pyupgrade, autoflake, isort) + recipe engine + pattern replay (structural hashing, fuzzy matching) + composite patterns + AST transforms, tried in order before LLM fallback. Structured telemetry per stage.
- **Recipe engine** — declarative YAML fix recipes with 16 built-in entries. SAFE/CAUTION/EXPERIMENTAL safety labels. Four handler types: text, regex, command, scaffold.
- **Fix confidence tiers** — T0 (guaranteed) through T4 (structural) with tiered validation. Auto-rollback on failure. Tier escalation for repeatedly-failing rules.
- **Pattern learning** — `FixPatternStore` with structural hashing and fuzzy matching. Cross-repo `SharedPatternLibrary`. `CompositePattern` for multi-step fixes. Auto-promotion of high-confidence patterns.
- **Batch PR system** — groups findings by rule/file. Conflict detection prevents overlapping edits. Failure-aware splitting with recursive retry. Crash recovery for interrupted batches.
- **PR automation** — issue-cycle (GitHub issues), pr-cycle (apply fixes + open PRs), merge-cycle (merge/rebase). Deduplication guard. `rebase-sweep` for sibling PRs.
- **Orchestration** — `pipeline.py` ties discovery → issues → fixes → PRs → merge into full sweep. Per-phase execution with locking.

### Autonomous Review

- **Review cycle engine** — GitHub-native review lifecycle. Observation, autonomous-review, and remediation modes.
- **Guarded live review** — shadow mode (dry-run), limited mode (guarded), local-only fallback. `MonitoredSafetyState` with circuit-breaker, failure counting, auto-rollback.
- **Publication pipeline** — candidate normalization, identity assignment, deduplication, eligibility checks, chunking, publishing with status tracking.
- **Feedback learning** — provider-style feedback capture, pattern confidence adjustment from review outcomes.

### Application Layer

- **Onboarding** — `bluei init` (interactive) and `bluei onboard` (headless). Language/framework/build-tool detection. Template-based config generation.
- **Health engine** — 0-100 vitality score per repo. Component-level scoring. Emergent rule inference. Baseline tracking.
- **State management** — JSONL-based persistence. Typed wrappers for `ReviewRun`/`FeedbackEvent`. Per-repo state isolation.
- **Configuration** — `ConfigManager` for repo configs/templates/rule packs. `config.yaml` for workspace settings.

### Advanced Capabilities

- **Directive seeding** — finding-level failure memory with exponential backoff cooldown. Prior context, fix history, rule history injected into LLM prompts.
- **Campaign orchestration** — batch-related fixes into ordered phases. Plan, dry-run, execute, pause, resume, abort. Dependency-aware ordering.
- **Emergent rule discovery** — proposes new detection rules from repeated finding patterns. Shadow-runs candidates. Confidence-gated promotion.
- **Push notifications** — Slack (Block Kit), email (SMTP), webhook, and log channel. `@register_notifier` decorator for extensions. Rate limiting per-finding and per-channel.
- **CI generation** — `bluei ci` generates GitHub Actions workflow from template.
- **AST engine** — Python, TypeScript, Go, Rust support. Pattern matchers for performance anti-patterns, hardcoded paths, broad exceptions. Safe AST transforms.
- **Auto-tuning** — adaptive detection thresholds from telemetry. Suppresses noisy rules, boosts high-yield rules.
- **Cost tracking** — per-cycle token counting across 9 models. Soft warning at $2/run, hard limit at $10/run. Pattern replay savings tracked.

### Infrastructure

- **Self-healing** — runs on every invocation. Cleans stale lock files, prunes orphaned worktrees, repairs corrupted state.
- **Reboot-resilient state** — worktree porcelain parsing, auto-prune, 6 corruption repair cases.
- **8 language plugins** — Python, TypeScript, Go, Rust, Shell, Dockerfile, Markdown, plus test fixture.
- **Canonical `bluei` package** — `bluei.app`, `bluei.engine`, `bluei.review`, `bluei.campaigns`.
- **5,497 tests** across 180+ test files. Mock GitHub API throughout.

---

## What's Next

### Near-term (v0.2.0)

- **Wire deferred features** — Mnemo cross-finding recall (`_get_mnemo_client`) and rule pack selection from templates (`select_rule_pack`).

### Medium-term

- **`engine/report.py` integration roadmap** (5 phases) — deduplicate helpers, wire app→engine pipeline, dashboard consumer, scheduled delivery, continuous monitoring.
- **`bluei notify config` wizard** — interactive scaffolding for `notifications.yaml`.

### Deferred (design decisions needed)

- **FixPatternStore O(n) rebuild** — append-only compaction vs sqlite vs in-memory. See `docs/plans/DEFERRED_ARCHITECTURAL.md` D1.
- **Multi-file composite rollback** — rollback scope strategy. See D2.
- **Non-Python AST cascade** — `ASTTransformCascadeStage` is Python-only. Separate architectural task.
- **`skip` strategy asymmetry** — `fix_strategy="skip"` → REFACTOR_CLASS vs `default_strategy="skip"` → CLAUDE_FIX.

### Shipped post v0.1.0-alpha.1

- **Structural refactoring** — cycle.py decomposition (C3), cli.py decomposition (C4), lifecycle singletons (H4), orchestrator 4-module split (S3), structural_hash package (S4), pipeline phase extraction (S5), batch_recovery extraction (S2), observation + autonomous cycle decomposition (rec-16, rec-18).
- **Merge-cycle audit closure** — all 21 items from `MERGE_CYCLE_AUDIT.md` fixed: DISMISSED review gate, observation state recovery, review-state race locking, merge-queue detection, HAS_HOOKS handling, worktree reuse validation, loop-count semantic guard, retry_pending_push escalation, configurable PR limits, language-aware candidate scanning, inter-PR rate throttling, atomic-write fsync, JSONL rotation, datetime cooldown comparison, generate_id entropy, runner finally-block protection.
- **E2E test suite** — 83 tests across 9 domains.
- **Notifications hardening** — digest severity filter, token masking, dashboard test split.

---

*Updated 2026-06-14*
