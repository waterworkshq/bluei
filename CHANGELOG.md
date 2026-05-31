# Changelog

All notable changes to `bluei` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
