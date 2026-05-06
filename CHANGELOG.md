# Changelog

All notable changes to `qa-agent` are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

### Changed
- **`qa_agent/review.py`** — review-cycle now treats clean PRs as **`pending_review`** until a qa-agent review artifact exists for the exact PR snapshot. A PR is no longer allowed to default straight to `merge_ready` just because GitHub shows a clean merge state and no visible blockers.
- **`qa_agent/review.py`** — review ingestion now considers both review threads and regular PR discussion comments, so strong non-thread review signals can drive remediation churn.
- **`qa_agent/review.py`** — review comment classification now treats blocking phrases like `not safe to merge`, `will still fail`, and similar language as actionable, while filtering review-bot status chatter and qa-agent self-comments.
- **`qa_agent/review.py`** — remediation worktree preparation now prefers fetching the PR head ref (`refs/pull/<n>/head`) before falling back to `origin/<branch>`, fixing invalid-reference failures in review-cycle churn.
- **`core/sandbox_local_runner/cli.py`** — merge-cycle now merges at most one PR per run, PRs that become `DIRTY` / `BEHIND` are triaged back into `pr-cycle` for branch repair instead of being left to drift, and autonomous `merge_ready` review artifacts can satisfy the merge gate even without a human `APPROVED` review.
- **`core/sandbox_local_runner/gh.py`** — open PRs for merge are now deterministically ordered oldest-first (non-draft first, then `createdAt`, then PR number) so autonomous merge progression is stable across runs.
- **`core/sandbox_local_runner/gh.py` / `linters.py` / `llm_fixable_rules.yaml`** — legacy ky refactor rule names (`max-lines`, `complexity`, `no-warning-comments`) now normalize into the active `xo-*` remediation flow, and previously-declared but dormant autonomous lanes (`type-untyped-import`, `test-coverage-line`) are now discoverable and routed through the LLM fix engine.

### Fixed
- **Review lifecycle trust gap** — managed PRs no longer appear merge-ready before qa-agent has actually reviewed them.
- **Review-cycle blind spot** — important PR-page comments from external review tools are now surfaced into remediation planning instead of being silently ignored.
- **Review-cycle churn entry** — PR #78-style review feedback can now reach `retry_prepared` / `retry_pending_push` with validated changes instead of dying on branch resolution.
- **Validation/no-op misclassification** — remediation runs that make no diff but fail validation now land in `retry_failed_validation` instead of being mislabeled `retry_no_changes`.
- **Review-state audit trail mismatch** — `review_state.last_action` now records the real final lifecycle status (`merge_ready`, `retry_exhausted`, etc.) instead of collapsing many states into `observed`.
- **Retry eligibility accounting** — exhausted or approval-blocked PRs no longer appear as `retry_eligible` in persisted state and review-care summaries.
- **Pytest discovery boundary** — root test runs now stay inside qa-agent’s own `tests/` tree instead of accidentally collecting nested repo/worktree tests under `repos/`.
- **Chunk ordering language priority** — `order_files_for_chunking()` now actually boosts files matching the repo’s primary language instead of treating all files as the same language when a hint is provided.
- **Merge-cycle drift window** — auto-merge no longer chains multiple PR merges in one run, merge-conflict / behind-base PRs are now returned to `pr-cycle` so the branch can be repaired before another merge attempt, sandbox PRs with a real qa-agent `merge_ready` artifact no longer stall forever on `no-reviews-block`, and merge selection order is now deterministic instead of whatever `gh pr list` happens to return.
- **Advanced-care capability drift** — the audit gaps between declared and functioning TS/JS care are narrower now: legacy ky rule names no longer strand existing issues outside the newer refactor flow, and `type-untyped-import` / `test-coverage-line` are no longer catalog-only dead ends.

### Tests
- Updated lifecycle regression coverage to assert the stricter `pending_review -> review artifact -> merge_ready` progression.
- Added regression coverage for validation-failed/no-diff remediation runs and for retry-eligibility semantics on exhausted or approval-blocked PRs.
- Updated state and obsidian-sync fixtures to use `pending_review` terminology instead of the older `awaiting_review` fixture state.
- Added root `pytest.ini` so `pytest -q` validates qa-agent itself instead of leaking into embedded runtime repos/worktrees.
- Added targeted merge-cycle regression coverage for single-merge-per-run behavior, merge-conflict triage back to `pr-cycle`, the autonomous review-artifact merge gate, and deterministic oldest-first PR ordering.
- Added capability-hardening regression coverage for legacy rule normalization, untyped-import discovery, and uncovered-line coverage discovery.

## [2.6.0] — 2026-04-01

### Added
- **LLM-fixable rules system** — routes non-autofixable findings to LLM fix engine when a rule-specific prompt hint is available, instead of skipping them entirely.
  - New config file: `core/sandbox_local_runner/llm_fixable_rules.yaml` — maps rules to prompt hints for LLM-assisted fixes.
  - New constant: `load_llm_fixable_rules()` in `constants.py` — loads rule definitions from YAML with fallback defaults.
  - New issue status: `needs-human-not-fixable` — assigned to findings that are neither autofixable nor LLM-fixable, excluded from actionable cap count.
- **`core/sandbox_local_runner/state.py`** — `NON_ACTIONABLE_ISSUE_STATUSES` now includes `needs-human-not-fixable`. New `count_actionable_issues()` function filters issues by actionable status.
- **Actionable issue cap** — pr-cycle now counts only actionable issues toward the cap, excluding blocked/escalated/resolved/unfixable issues. This prevents stale or unfixable issues from blocking new work.
- **`tests/test_llm_fixable_rules.py`** — 11 tests covering config loading, status routing, and rule set overlap checks.

### Changed
- **`core/sandbox_local_runner/cli.py`** — pr-cycle filter now routes `safe_to_autofix=False` findings through LLM fix engine if the rule is in `LLM_FIXABLE_RULES`, with rule-specific prompt hints injected via `extra_prompt` parameter. Findings not in any fixable set are marked `needs-human-not-fixable`.
- **`core/sandbox_local_runner/lifecycle.py`** — `apply_claude_fix()` accepts optional `extra_prompt` parameter for rule-specific guidance injection into the fix prompt.

### Initial LLM-fixable rules
- `ruff-b904` — bare `raise` without cause (40 stuck issues in zulip backlog)
- `ruff-s311` — stdlib `random` in security-sensitive context

## [2.5.0] — 2026-03-22

### Added
- **`scripts/run_and_sync.sh`** — wrapper script that runs a qa-agent phase then deterministically syncs Obsidian records. All host crontab entries now route through this wrapper instead of calling `qa-agent run` directly.
- **`qa_agent/healer.py`** — `TransientArtifactHealer` module providing self-healing for dirty worktrees caused by common generated artifacts (`__pycache__/`, `coverage/`, `.ruff_cache/`, `.nyc_output/`, `node_modules/`, etc.).
- **`qa-agent heal` CLI command** — manual operator command for healing dirty worktrees (`--dry-run`, `--no-dry-run`, `--remove-artifacts`, `--force`).
- **`scripts/obsidian_sync.py`** — deterministic script that reads qa-agent state/run artifacts and writes per-repo sections into date-stamped Obsidian log files in `~/Obsidian/Logs/{issue-cycle,pr-cycle,merge-cycle,qa-monitor}/YYYY-MM-DD.md`.
- **`scripts/daily_summary.py`** — generates per-repo markdown summaries in `~/Obsidian/Logs/qa-daily/<repo>-YYYY-MM-DD.md` from current qa-agent state.
- **`tests/test_healer.py`** — regression tests for healer logic (pattern catalog, transient detection, gitignore healing, safe-to-autoheal, preflight integration).
- **`tests/test_obsidian_sync.py`** — integration tests for obsidian sync and daily summary scripts (idempotent writes, mixed-format handling, multi-repo per file).
- **`tests/test_safety_mode_scheduling.py`** — tests for safety-mode-aware cron scheduling (ISSUE_ONLY gets review-cycle, PR mode excludes merge-cycle, etc.).
- **`tests/test_issue_cycle_untracked_findings.py`** — regression tests ensuring missing-file findings can create GitHub issues in issue-cycle while remaining blocked in pr-cycle.
- **`tests/test_models.py`** — tests for standalone functions (`generate_id`, `now_iso`) and model round-trips.
- **`tests/test_config.py`** — tests for ConfigManager and RepoConfig migration/safety defaults.
- **`tests/test_report.py`** — tests for ReportGenerator markdown output, score bands, and review care section.

### Changed
- **`scripts/install-cron.sh`** — now installs cron entries pointing at `run_and_sync.sh` instead of `qa-agent` directly.
- **`install-cron` scheduling logic** — now respects `INCLUDE_PR`, `INCLUDE_REVIEW`, `INCLUDE_MERGE` flags set from repo safety mode:
  - `ISSUE_ONLY`: issue-cycle + review-cycle only
  - `PR`: issue-cycle + pr-cycle + review-cycle (no merge-cycle)
  - `MERGE`: all phases
- **`qa-agent preflight`** — now reports when a dirty worktree is exclusively transient artifacts and would be auto-healable.
- **`qa-agent onboard`** — auto-heals transient artifacts before onboarding when the worktree is exclusively transient.
- **`qa-agent run`** — auto-heals before executing when `require_clean_worktree=True` and tree is exclusively transient.
- **`core/sandbox_local_runner.py`** — fixed tracked-path filter gate: changed `if args.live_github_actions:` to `if args.live_github_actions and run_pr_cycle:` so missing-file findings can create issues in issue-cycle without being suppressed by the PR-phase safety guard.
- **`obsidian_sync.py` and `daily_summary.py`** — now use `zoneinfo.ZoneInfo("Asia/Kolkata")` for IST timestamps instead of naive `astimezone()`.

### Fixed
- **`scripts/obsidian_sync.py`** — `review-cycle` was not a valid phase, causing every `review-cycle` cron run to fail at the obsidian sync step. Added `review-cycle` to `CYCLE_SUBDIRS`, `REPO_HEADERS`, `_phase_content` builders, and implemented `_build_review_cycle()`. The review-cycle phase now correctly syncs to `~/Obsidian/Logs/review-cycle/`.
- **`scripts/daily_summary.py`** and **`scripts/obsidian_sync.py`** — both scripts were reading `open_issues` and `open_prs` from `status.json`'s `current_counts` which was stale. Fixed to derive these counts directly from `issues.json` and `active_prs.json`.
- **`scripts/daily_summary.py`** — `PRs Created` in the daily activity table was summing `prs_created` across all phases including `review-cycle`. But `review-cycle`'s `prs_created` reflects PRs under review care management, not newly opened PRs — inflating the count to 32 for ky. Fixed: only sum `prs_created` from `issue-cycle` and `pr-cycle` runs.

### Fixed
- `zulip` repo: was scheduling `merge-cycle` despite being in `pr` safety mode (impossible phase never succeeds, only logs noise).
- `zulip` repo: dirty worktree from generated `coverage/` and `.nyc_output/` was blocking all live execution.
- Missing-file findings (`test-gap-missing-file` rule) were being silently suppressed in live issue-cycle runs because the untracked-path filter was too broad.

---

## [2.4.0] — 2026-03-12

### Added
- **Template-aware config system** — structured repo templates in `templates/repos/*.yaml` with `meta.onboarding_version`, `meta.template`, `meta.inferred_by` provenance fields.
- **Node/TypeScript templates** — `node-library`, `node-api`, `react-app`, `next-app`.
- **Python templates** — `python-library`, `python-api`, `django-app`, `fastapi-app`.
- **Go and Rust support** — `plugin-go`, `plugin-rust`, `go-service`, `rust-crate`.
- **Monorepo/workspace handling** — `node-workspace-root` / `node-package-in-workspace` templates.
- **First-class safety policy** — `RepoConfig.safety.mode` (`observe|issue-only|pr|merge`) and `safety.profile` (`conservative|balanced|aggressive`).
- **Runtime safety enforcement** — observe mode blocks live runs; issue-only blocks PR/merge; PR blocks merge; dirty worktree blocks live when enabled.

### Changed
- `ky` config updated with explicit `mode: merge, profile: balanced`.
- Onboarding now auto-selects template and infers package manager, build tool, baseline checks, Docker hints, backend defaults.

---

## [2.3.0] — 2026-03-08

### Added
- **Review care system** — autonomous PR management: retry preparation, remediation execution, push boundary, loop detection.
- **`qa-agent review` command** — review diagnostics, care status, PR detail views.
- **End-to-end review lifecycle tests** (`test_review_lifecycle.py`).
- **Review diagnostics tests** (`test_review_diagnostics.py`).

---

## [2.2.0] — 2026-03-07

### Added
- **Standalone `sandbox_local_runner.py`** — moved execution authority to host-side runner (not OpenClaw session).
- **`qa_agent/health.py` v2** — granular component scores (bug_quality, lint_quality, technical_debt, documentation, performance, test_gaps, test_coverage, type_safety, maintainability).
- **`health_history.jsonl`** — persisted health snapshots per repo.

### Changed
- Architecture shifted from OpenClaw Phase2 session ownership to standalone host-side qa-agent.

---

## [2.1.0] — 2026-03-06

### Added
- **`qa-agent` standalone bootstrap** — `scripts/bootstrap.sh`, `docs/INSTALL.md`.
- **Generic host cron installer** — `scripts/install-cron.sh` + `qa-agent install-cron` CLI.
- **Per-phase locking** — lock files under `qa-agent/locks/` prevent concurrent same-phase runs.

---

## [2.0.0] — 2026-03-05

### Added
- Full rewrite: `qa_agent/` package, `core/` runner, `templates/`, `plugins/`, `tests/`.
- First release tracked in this changelog.
