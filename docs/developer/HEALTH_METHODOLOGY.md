<!-- CATEGORY: developer -->

# Health Scoring & Observability

## Overview

bluei computes a **0–100 composite health score** for each onboarded repository, derived from
findings produced by language-specific linters and structured into nine weighted components.
The score is tracked over time in `health_history.jsonl`, visualized through an observability
dashboard, and surfaced via five report formats. A separate cost-tracking subsystem monitors
per-invocation model API spend with soft/hard limits.

**Related docs:** [ARCHITECTURE.md](ARCHITECTURE.md), [OPERATOR_GUIDE.md](../operator/OPERATOR_GUIDE.md), [CONFIG_REFERENCE.md](../reference/CONFIG_REFERENCE.md).

---

## Health Scoring

### 0–100 Composite Score

The composite score is a **weighted sum** of nine granular component scores, each computed
independently from the findings mapped to that component. The formula (in `HealthEngine.calculate`,
`bluei/app/health.py:295–355`) is:

```
base_score = Σ(component_score × component_weight)   for all 9 components
```

### Component Breakdown

Each component has a default weight and maps to a specific number of detection rules:

| Component | Weight | Rules | What it captures |
|-----------|--------|-------|------------------|
| `bug_quality` | 0.20 | 6 | Potential bugs (discount math, query normalization, tax truncation, etc.) |
| `lint_quality` | 0.05 | 3 | Style issues (broad except, hardcoded paths, trailing whitespace) |
| `technical_debt` | 0.05 | 1 | TODO markers and deferred work |
| `documentation` | 0.10 | 5 | Missing docs, stale references, doc gaps |
| `performance` | 0.10 | 2 | Inefficient patterns (pop-front loop, list membership loop) |
| `test_gaps` | 0.10 | 2 | Missing test files or missing test cases |
| `test_coverage` | 0.15 | 3 | Branch, function, and line coverage gaps |
| `type_safety` | 0.15 | 4 | Explicit `any`, missing return/param types, untyped imports |
| `maintainability` | 0.10 | 2 | Refactor needs (max-lines, complexity) |

Weights are defined in the `HealthWeights` dataclass (`bluei/app/health.py:14–60`). A legacy
`code_quality` aggregate exists for backward compatibility, computed as the weighted average of
`bug_quality` + `lint_quality` + `technical_debt` + `maintainability`.

### How Findings Map to Components

The mapping is a three-tier lookup in `HealthEngine._get_component()` (`health.py:205–233`):

1. **Direct rule map** (`RULE_TO_COMPONENT`): 28 built-in rules → exact component match.
   Example: `discount-math-sign` → `bug_quality`, `type-explicit-any` → `type_safety`.

2. **Language-pack mappings** (`health_mapping.yaml`): Each plugin (`plugins/<lang>/health_mapping.yaml`)
   extends the engine's mapping at load time via `HealthEngine.register_language_pack_mappings()`.
   For example, `plugins/typescript/health_mapping.yaml` adds `debug-console-log` → `lint_quality`.

3. **Category inference** (`CATEGORY_INFERENCE`): If the rule is not in either map, the engine
   checks rule-name keywords (`test` → `test_gaps`, `coverage` → `test_coverage`, `doc` →
   `documentation`, `todo`/`fixme` → `technical_debt`, etc.).

4. **Default fallback**: Unknown rules default to `bug_quality`.

**Emergent rule support**: Rules prefixed with `emergent:` are first checked against the active
emergent rules list for a `component_override`; they fall back to `bug_quality` if not found.

### Finding Severity vs. Confidence

Severity is inferred from the finding's `confidence` field (`health.py:194–203`):

| Confidence | Severity |
|------------|----------|
| ≥ 0.90 | `critical` |
| ≥ 0.85 | `high` |
| ≥ 0.75 | `medium` |
| < 0.75 | `low` |

### Logarithmic Penalty Scaling

Component scores use **logarithmic scaling** to prevent score collapse when many findings exist
for one component (`health.py:235–293`):

```
total_penalty = Σ (base_penalty[severity] × count/5 × log₁₀(count + 1))
component_score = max(5.0, 100 - total_penalty)   # floor at 5.0
```

The per-severity base penalty is component-specific (`SEVERITY_PENALTY`, `health.py:152–162`):

| Component | Critical | High | Medium | Low |
|-----------|----------|------|--------|-----|
| `bug_quality` | 20 | 12 | 6 | 2 |
| `performance` | 15 | 8 | 3 | 1 |
| `test_gaps` | 12 | 6 | 3 | 1 |
| `type_safety` | 12 | 6 | 2 | 0.5 |
| `test_coverage` | 10 | 5 | 2 | 0.5 |
| `technical_debt` | 8 | 5 | 2 | 0.5 |
| `lint_quality` | 5 | 3 | 1 | 0.25 |
| `documentation` | 5 | 3 | 1.5 | 0.5 |
| `maintainability` | 5 | 3 | 1 | 0.25 |

This means 5 critical `bug_quality` findings apply a ~16-point penalty (20 × 1 × log₁₀(6) ≈ 15.6),
while 50 findings apply ~171 points but get clamped to a floor of 5.

### Component Score Overrides

- **Coverage data**: If actual coverage data is provided (via `coverage_data['percentage']`),
  the `test_coverage` component is directly overridden with the measured value
  (`health.py:327–328`).

- **Baseline improvement bonus**: When a `baseline_score` is provided and current score exceeds
  it, a bonus of up to **5 points** is added (`improvement × 0.1`, capped at 5.0)
  (`health.py:344–349`).

### Score Bands

Defined on the `HealthScore` dataclass in `bluei/app/models.py:494–515`:

| Band | Range | Color | Description |
|------|-------|-------|-------------|
| `excellent` | 90–100 | green | Clean codebase, few or no findings |
| `good` | 70–89 | blue | Some minor issues, generally healthy |
| `needs_work` | 50–69 | yellow | Accumulating debt; target for improvement |
| `poor` | 30–49 | orange | Significant issues across multiple components |
| `critical` | 0–29 | red | Widespread problems; urgent attention needed |

### Health History Tracking

Snapshots are persisted via `HealthEngine.save_health_snapshot()` (`health.py:463–479`) to
`state/health_history.jsonl` in JSONL format. A separate file, `health_trend.jsonl`,
is written by the runner each cycle and provides the longer-running trend view used
by the dashboard (see [dashboard data ingestion](#dashboard-data)).

```json
{"timestamp": "2026-05-23T10:00:00.000Z", "score": 78.3, "components": {"bug_quality": 92.0, ...}, "findings_count": 15}
```

Retrieval via `get_health_history()` (`health.py:481–497`) loads all entries and clips to the
requested day window.

### Priority Issues

`HealthEngine.prioritize_issues()` (`health.py:390–443`) ranks findings by a priority score:

```
priority_score = component_weight × severity_multiplier × quick_win_bonus × confidence
```

where `severity_multiplier` is: critical=4, high=3, medium=2, low=1, and `quick_win_bonus` is
1.5 if the finding is safe to autofix. The top N findings are returned sorted by priority score.

### Baselines

Created via `HealthEngine.create_baseline()` (`health.py:445–461`). A `Baseline` (`models.py:527–541`)
captures snapshot data:

| Field | Description |
|-------|-------------|
| `id` | Generated UUID (e.g., `baseline-20260523-a1b2c3d4`) |
| `repo_id` | Repository identifier |
| `captured_at` | ISO timestamp of capture |
| `findings_total` | Total findings at baseline |
| `findings_by_category` | Per-component finding counts |
| `findings_by_severity` | Per-severity counts (critical/high/medium/low) |
| `health_score` | Composite score at baseline |
| `health_components` | All 9 component scores |

---

## Dashboard

The dashboard is a **read-only, self-contained HTML page** generated from per-repo state files
by `bluei/app/dashboard.py`. It does **not** use Chart.js — it renders inline SVG sparklines and
CSS-styled tables for a zero-dependency experience.

### Key Functions

| Function | File | Purpose |
|----------|------|---------|
| `build_dashboard_data()` | `dashboard.py:40` | Aggregates all repos' state into a structured dict |
| `write_dashboard()` | `dashboard.py:70` | Entry point; builds data, renders HTML, scrubs secrets, writes to disk |
| `render_dashboard_html()` | `dashboard.py:114` | Produces the full HTML document with embedded CSS/JS |
| `_build_repo_summary()` | `dashboard.py:280` | Reads a single repo's `state/` directory into a summary dict |

### HTML Output Structure

The dashboard renders seven tabbed panels:

| Tab | ID | Content |
|-----|-----|---------|
| **All** | — | All sections visible simultaneously |
| **Fleet** | `section-fleet` | Sortable table: repo name, language, vitality score, trend, open findings, last run |
| **Learning** | `section-learning` | Per-repo cards with SVG sparklines, fix pattern counts, emergent rule stats |
| **Campaigns** | `section-campaigns` | Campaign progress bars with completion percentage |
| **Review** | `section-review` | Review cycle health: publication rate, retry failures, auto-tune mode, rebase success rate |
| **Escalations** | `section-escalations` | Aggregated escalation feed table (repo, reason, severity badge, timestamp) |
| **Raw State** | `section-raw` | Per-repo state file inventory with entry counts and byte sizes |

### Visualizations (No Chart.js)

- **SVG sparklines**: Rendered inline in `_render_sparkline()` (`dashboard.py:664`) using
  `<polyline>` and `<circle>` elements. Points above previous value are colored green
  (`var(--accent)`), decline points are red (`var(--bad)`).
- **Vitality score table**: Last 5 history entries with color-coded scores and timestamps
  (green ≥ 75, yellow ≥ 50, red < 50).
- **Progress bars**: CSS `<div>` bars for campaign completion.
- **Severity badges**: Color-coded inline spans (red for critical, yellow for high, blue for medium).

### Data Ingestion

`_build_repo_summary()` reads from these state files:

| State File | Aggregated Into |
|------------|-----------------|
| `health_trend.jsonl` | Health history, current vitality, trend |
| `findings.jsonl` | Open findings count |
| `config.yaml` | Language, repo metadata |
| `fix_patterns.jsonl` | Active fix pattern count, top-10 by success |
| `emergent_rules.json` | Status counts, worst false-positive rate |
| `campaigns/*/campaign.json` | Campaign progress, status |
| `review_stats.jsonl` | Publication rate, findings detected/published, retry failures |
| `auto_tune.json` | Auto-tune mode, override count |
| `cycle_signals.json` | Suppressed rule count |
| `rebase_stats.jsonl` | Rebase success/failure tracking |
| `escalation_log.jsonl` | Recent 100 escalations |

### Safety Features

- **Secret redaction**: `_scan_for_secrets()` (`dashboard.py:33`) strips GitHub tokens (`ghp_*`,
  `gho_*`, `ghs_*`, `ghu_*`), Stripe keys (`sk_*`), AWS keys (`AKIA*`), and Slack tokens from
  HTML output before writing.
- **5 MB file size cap**: If HTML exceeds 5 MB, health history is truncated to 10 entries per
  repo and re-rendered.
- **Read-only**: The dashboard purely reads state files; it never modifies any state.

### Output Formats

- **HTML** (default): Self-contained page with inline CSS/JS, no external dependencies.
- **JSON**: Raw aggregated data via `json.dumps()`.

---

## Reports

### Five Formats

Reports are generated by `bluei/app/report.py` (`ReportGenerator`) and the `bluei` CLI
dispatcher in `bin/bluei.py`. Different format code paths are used:

| Format | Output | Code Path |
|--------|--------|-----------|
| **text** | Terminal-friendly plain text | `_text_report_summary()` in `bin/bluei.py` |
| **WhatsApp** | Compact, emoji-free plain text | `_text_report_summary()` in `bin/bluei.py` |
| **JSON** | Structured JSON data model | `bluei/engine/report.py:extract_report_data()` |
| **HTML** | Chart.js dashboard report | `bluei/engine/report.py:generate_report_html()` |
| **PDF** | PDF (default) | `bluei/app/report.py:ReportGenerator.generate_pdf()` |

### Text Format

Plain text summary generated by `_text_report_summary()` in `bin/bluei.py`:

- Vitality score, findings count, open issues
- Recent runs summary (verified fixes, PRs created, recent failures)
- Review care diagnostics (active/blocked/pending-push/exhausted/ready PR counts)

### WhatsApp Format

Same code path as text — a compact single-block summary suitable for messaging platforms.
No emojis, just `[good]`/`[fair]`/`[poor]` labels.

### JSON Format

Delegates to `bluei/engine/report.py:extract_report_data()`. Builds a canonical
JSON data model from:

- `status.json` (current counts, health score)
- `issues.json` (open/closed issues)
- `findings.jsonl` (all findings with rule→category mapping)
- `health_history.jsonl` (trend data)
- `state.json` (reconciliation events)

The output JSON structure includes:

```json
{
  "repo": { "name": "...", "path": "...", "language": "...", "health_score": 78.3, ... },
  "counts": { "total_findings": 15, "open_issues": 3, "open_prs": 2, "findings_fixed": 42 },
  "findings_by_category": { "bug_quality": 4, "lint_quality": 2, ... },
  "health_trend": [{"date": "2026-05-01", "score": 72.0}, ...],
  "top_rules": [{"rule": "type-explicit-any", "category": "type_safety", "count": 8, "language": "typescript"}, ...],
  "language_distribution": { "typescript": 45, "python": 12 },
  "generated_at": "2026-05-23T..."
}
```

### HTML Format

Delegates to `bluei/engine/report.py:generate_report_html()`. Uses the template
`bluei_report_template.html` (at repo root) which includes Chart.js 4.4.7 via CDN.

**Charts rendered:**

- **Vitality gauge**: Canvas-drawn arc gauge with red→yellow→green gradient
- **Category doughnut chart**: Breakdown of findings by category
- **Trend line chart**: 30-day health score trend with golden fill
- **Language distribution bar chart**: Horizontal bars per language

**Other content:**

- Summary cards (total specks, open issues, open PRs, issues fixed)
- Top rules table (rule name, category badge, count, language)

If the template is not available, a fallback inline HTML (`_build_fallback_html()`) is
generated.

### PDF Format

`ReportGenerator.generate_pdf()` (`bluei/app/report.py:274–332`):

1. Generates a **markdown report** via `generate_markdown_report()` containing:
   - Executive summary with vitality score and band
   - Change from baseline (if available)
   - 9 vitality components with ASCII bar charts (`███░░░░░`)
   - Findings breakdown table by category
   - Health history table (last 10 entries)
   - Review care status (if active PRs exist)
   - Metrics summary (total specks, fixes, PRs, merges)
2. Converts markdown to PDF via the external `pdf-report` skill located at
   `$QA_AGENT_WORKSPACE/../skills/pdf-report/scripts/generate_pdf.py`.
3. If the PDF skill is not available, falls back to saving the markdown.

### Report Template

`bluei_report_template.html` (407 lines) defines a dark-themed report with the "bluei brand"
color palette:

- **Abyss** (`#060a0e`): page background
- **Breach** (`#fa746f`): error/bug color
- **Krill** (`#f0c060`): warning/accent color
- **Dorsal** (`#2a5f73`): info color
- **Glass** (`rgba(17,26,36,0.7)`): card backgrounds with backdrop blur

The template's `DATA` object is replaced at generation time with actual values from
`extract_report_data()`.

---

## Cost Tracking

Per-invocation model API cost tracking is handled by `bluei/engine/cost_tracker.py`
(`CostTracker`). It is independent of the health scoring system but is consumed by the
`bluei/engine/health.py` enrichment function.

### Model Rates (`MODEL_RATES`)

Nine rate entries defined in `cost_tracker.py:20–58` (USD per 1K tokens):

| Model | Input / 1K | Output / 1K |
|-------|-----------|-------------|
| `claude-sonnet-4` | $0.003 | $0.015 |
| `claude-sonnet-4-20250514` | $0.003 | $0.015 |
| `claude-3-5-sonnet-20241022` | $0.003 | $0.015 |
| `claude-opus-4` | $0.015 | $0.075 |
| `claude-3-haiku-20240307` | $0.00025 | $0.00125 |
| `claude-3-opus-20240229` | $0.015 | $0.075 |
| `gpt-4o` | $0.005 | $0.015 |
| `gpt-4o-mini` | $0.00015 | $0.0006 |
| `default` (fallback) | $0.003 | $0.015 |

### Thresholds

Hardcoded in `CostTracker.__init__()` (`cost_tracker.py:70–92`):

| Threshold | Default | Behavior |
|-----------|---------|----------|
| **soft_warn** | $2.00 | `warned()` returns `True`; callers log a warning but continue |
| **hard_limit** | $10.00 | `exceeded_limit()` returns `True`; callers should **skip** further model invocations |

### Cost Log Structure (`cost_log.jsonl`)

Each invocation is a JSONL line:

```json
{
  "timestamp": "2026-05-23T10:01:00.000Z",
  "model": "claude-sonnet-4",
  "input_tokens": 4500,
  "output_tokens": 1200,
  "cost": 0.0315,
  "cycle_total_so_far": 0.2415
}
```

### Pattern Replay Savings

`CostTracker.record_pattern_replay_savings()` (`cost_tracker.py:157–188`) writes savings entries
to the same log file. These entries have `"type": "pattern_replay_savings"` and track estimated
cost avoided when a deterministic pattern replay replaces an LLM call. Savings entries do **not**
affect `cycle_total()`, soft-warn, or hard-limit thresholds.

### Cost Summarization

`CostTracker.summary()` (`cost_tracker.py:244–291`) returns:

- `total_cost`: aggregate spend
- `total_invocations`: count (excluding savings records)
- `per_model`: per-model count and cost breakdown
- `earliest` / `latest` timestamps
- `pattern_replay_savings`: count and total saved (if any savings exist)

### Health Enrichment

`bluei/engine/health.py:enrich_health_with_cost()` reads `cost_log.jsonl` and
adds a `cost` key to any health summary dict with:

- `total_cost`, `total_invocations`, `avg_cost_per_run`
- `per_model` breakdown

---

## Commands

### `bluei health`

```
bluei health <project-name> [--days <n>] [--format text|whatsapp]
```

Shows the vitality score (0–100) for a project. Displays score, recent history (last 5 entries),
and finding counts. WhatsApp format provides compact output.

### `bluei report`

```
bluei report <project-name> [--format pdf|text|json|html|whatsapp] [-o <path>] [--days <n>]
```

Generates a comprehensive report. Default format is PDF. Output path defaults to
`reports/<name>-report.{pdf,html}`. All five formats include vitality scores, findings
breakdowns, and health history.

### `bluei dashboard`

```
bluei dashboard [--repo <name>] [--format html|json] [-o <path>] [--state-root <dir>] [--open]
```

Generates a read-only, self-contained observability dashboard from all per-repo state files.

- `--repo <name>`: Limit to a single repo.
- `--format json`: Raw JSON output instead of HTML.
- `--open`: Open in default browser after writing.
- `--state-root <dir>`: Override state directory (for testing).

---

## Module Index

| Module | Path | Responsibility |
|--------|------|----------------|
| `HealthEngine` | `bluei/app/health.py` | Core scoring engine: component calculation, severity mapping, baselines, history, prioritization |
| `HealthWeights` | `bluei/app/health.py` | Weight configuration dataclass with legacy `code_quality` backward-compat |
| `HealthScore` | `bluei/app/models.py` | Dataclass with `band` and `color` properties |
| `Baseline` | `bluei/app/models.py` | Snapshot of health state at onboarding |
| `PriorityIssue` | `bluei/app/health.py` | Ranked issue with priority score, urgency, and reason |
| `ReportGenerator` | `bluei/app/report.py` | Markdown and PDF report generation |
| `extract_report_data()` | `bluei/engine/report.py` | Canonical JSON extraction for reports |
| `generate_report_html()` | `bluei/engine/report.py` | Chart.js HTML report rendering |
| `build_dashboard_data()` | `bluei/app/dashboard.py` | Dashboard data aggregation |
| `write_dashboard()` | `bluei/app/dashboard.py` | Dashboard HTML/JSON writer with secret scanning |
| `render_dashboard_html()` | `bluei/app/dashboard.py` | Full HTML output with SVG sparklines |
| `CostTracker` | `bluei/engine/cost_tracker.py` | Per-invocation cost tracking with soft/hard limits |
| `enrich_health_with_cost()` | `bluei/engine/health.py` | Cost metrics enrichment for health summaries |
| `health_mapping.yaml` | `plugins/<lang>/` | Per-language rule→component mappings |

---

## Report Format Structure

bluei generates reports in 5 formats, each with a specific use case:

| Format | Command | Output | Use Case |
|--------|---------|--------|----------|
| **PDF** | `bluei report <repo>` (default) | PDF file with charts | Shareable stakeholder reports |
| **HTML** | `bluei report <repo> --format html` | Standalone HTML with Chart.js | Interactive dashboard, dark-themed, vitality charts |
| **JSON** | `bluei report <repo> --format json` | Machine-readable JSON | Programmatic consumption, CI integration |
| **Text** | `bluei report <repo> --format text` | Plain text | Terminal output, email body |
| **WhatsApp** | `bluei health <repo> --format whatsapp` | Compact text | Mobile-friendly health summary |

**HTML report structure** (from `bluei_report_template.html`):

- Dark-themed standalone page (no external dependencies)
- Chart.js vitality chart (health score over time)
- Finding breakdown by category with severity counts
- PR status summary
- Review care metrics
- Custom output path via `-o report.html`

**Health history format** (`health_history.jsonl`):

- Each line is a JSON snapshot with all 9 component scores, overall score, and ISO timestamp
- Default health output uses the `display_health()` formatter from `brand.py`
- WhatsApp format strips rich elements for plain-text delivery
