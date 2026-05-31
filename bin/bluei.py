#!/usr/bin/env python3
"""
bluei — Trust the silence.

Thin, smart dispatcher. Handles venv bootstrap, lexicon mapping,
help text, and dispatch — then hands off to the engine.
"""

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# ── Resolve paths ──────────────────────────────────────────────
BLUEI_DIR = Path(__file__).resolve().parent.parent
ENGINE_MODULE = "bluei.engine"
VENV_PYTHON = BLUEI_DIR / ".venv" / "bin" / "python3"
BLUEI_SECRETS = BLUEI_DIR / "bin" / "bluei_secrets.py"
if str(BLUEI_DIR) in sys.path:
    sys.path.remove(str(BLUEI_DIR))
sys.path.insert(0, str(BLUEI_DIR))

from bluei.version import __version__ as BLUEI_VERSION

try:
    from bluei.campaigns.types import CampaignStatus, PhaseStatus
except ImportError:
    CampaignStatus = None
    PhaseStatus = None


# ── Colors (no-op if not a TTY) ────────────────────────────────
if sys.stderr.isatty():
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    RESET = "\033[0m"
else:
    BOLD = DIM = CYAN = GREEN = YELLOW = RED = RESET = ""


# ── Venv bootstrap ──────────────────────────────────────────────
def _has_venv() -> bool:
    return VENV_PYTHON.exists()


def _python_works() -> bool:
    try:
        subprocess.run([str(VENV_PYTHON), "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _bootstrap() -> bool:
    print("bluei: Python environment not found, running bootstrap...", file=sys.stderr)
    bootstrap_script = BLUEI_DIR / "scripts" / "bootstrap.sh"
    try:
        subprocess.run(["bash", str(bootstrap_script)], cwd=str(BLUEI_DIR), check=True)
        return True
    except subprocess.CalledProcessError:
        print(
            f"bluei: bootstrap failed. Run 'bash {bootstrap_script}' manually.",
            file=sys.stderr,
        )
        return False


def _ensure_python() -> str:
    """Return a working python3 path. Bootstraps if needed."""
    if _has_venv() and _python_works():
        return str(VENV_PYTHON)
    if not _has_venv() and _bootstrap() and _python_works():
        return str(VENV_PYTHON)
    for candidate in ["python3", "python"]:
        try:
            subprocess.run([candidate, "--version"], capture_output=True, text=True)
            return candidate
        except FileNotFoundError:
            continue
    print("bluei: no Python interpreter found.", file=sys.stderr)
    sys.exit(1)


def _load_review_care_diagnostics(state, repo_name: str) -> dict:
    """Build review-care diagnostics from persisted active/review state."""
    active_state = state.load_active_prs(repo_name)
    review_state = state.load_review_state(repo_name)
    prs = active_state.get("prs", {}) if isinstance(active_state, dict) else {}
    review_prs = review_state.get("prs", {}) if isinstance(review_state, dict) else {}

    pending_push = []
    failed_push = []
    for pr_key, pr in prs.items():
        execution = pr.get("execution_result") or {}
        push_result = execution.get("push_result") or {}
        review = review_prs.get(str(pr_key), {})
        detail = {
            "pr_number": pr.get("pr_number") or int(pr_key),
            "branch": pr.get("branch", ""),
            "changed_files": execution.get("changed_files", []),
            "push_target_branch": push_result.get("target_branch", ""),
            "attempts_used": review.get("attempts_used", 0),
            "reason": review.get("last_action_reason", ""),
        }
        if pr.get("status") == "retry_pending_push":
            pending_push.append(detail)
        elif pr.get("status") == "retry_failed_push":
            failed_push.append(detail)

    return {
        "pending_push_prs": len(pending_push),
        "failed_push_prs": len(failed_push),
        "pending_push_prs_detail": pending_push,
        "failed_push_prs_detail": failed_push,
    }


# ── Help text system ───────────────────────────────────────────
HELP_TEXT = {
    "bluei": f"""{BOLD}bluei{RESET} {DIM}— Trust the silence.{RESET}

{BOLD}Usage:{RESET}
  {GREEN}bluei <command>{RESET} [{YELLOW}options{RESET}]
  {GREEN}bluei help{RESET} [{CYAN}<command>{RESET}]       Show help for a specific command

{BOLD}Commands:{RESET}

  {GREEN}init{RESET}             {DIM}Register a project (interactive or --path){RESET}
  {GREEN}onboard{RESET}          {DIM}Headless onboarding for a new project{RESET}
  {GREEN}install{RESET}          {DIM}Print installation/setup instructions{RESET}
  {GREEN}setup{RESET}            {DIM}Alias for install{RESET}
  {GREEN}update{RESET}           {DIM}Self-update to the latest version{RESET}

  {GREEN}status{RESET}     <name> {DIM}Show agent or project status{RESET}
  {GREEN}scan{RESET}       <name> {DIM}Run discovery/issue-cycle scan on a project{RESET}
  {GREEN}clean{RESET}       <name> {DIM}Fix specks and open pull requests{RESET}
  {GREEN}duster{RESET}     <name> {DIM}Dry-run a scan — preview specks, no changes{RESET}
  {GREEN}doctor{RESET}     <name> {DIM}Run operational checks/diagnostics{RESET}
  {GREEN}run{RESET}        <name> {DIM}Run full orchestrated sweep{RESET}
  {GREEN}secrets{RESET}      <path> {DIM}Scan for hardcoded secrets{RESET}
  {GREEN}health{RESET}     <name> {DIM}Show vitality (0–100){RESET}
  {GREEN}report{RESET}     <name> {DIM}Generate a vitality report (PDF by default){RESET}
  {GREEN}dashboard{RESET}        {DIM}Generate a read-only observability dashboard{RESET}
  {GREEN}languages{RESET}        {DIM}List installed language packs and rules{RESET}
  {GREEN}preflight{RESET}  <name> {DIM}Assess a project before registering{RESET}
  {GREEN}heal{RESET}       <name> {DIM}Heal dirty worktrees from transient artifacts{RESET}

  {GREEN}ci{RESET}        <name> {DIM}Generate GitHub Actions CI workflow{RESET}
  {GREEN}install-hook{RESET}  <name> {DIM}Install pre-commit hook{RESET}
  {GREEN}install-cron{RESET}  <name> {DIM}Install host cron schedule for project cycles{RESET}

  {GREEN}patterns{RESET}            {DIM}Inspect and manage learned fix patterns{RESET}
  {GREEN}campaign{RESET}            {DIM}Plan multi-finding refactor campaigns{RESET}
  {GREEN}emergent{RESET}            {DIM}Inspect proposed emergent rules{RESET}

{BOLD}Examples:{RESET}
  bluei init                     {DIM}# Register any project (wizard or --path){RESET}
  bluei init --path ~/my-project {DIM}# Register a specific project{RESET}
  bluei install                  {DIM}# Show install instructions{RESET}
  bluei scan diet-app            {DIM}# Scan a project for specks{RESET}
  bluei clean workout-app          {DIM}# Fix specks and open PRs{RESET}
  bluei health diet-app          {DIM}# Show vitality{RESET}
  bluei report diet-app          {DIM}# Generate PDF report{RESET}
  bluei secrets ./src            {DIM}# Scan for secrets{RESET}

Run {BOLD}bluei help{RESET} [{CYAN}<command>{RESET}] for detailed help on any command.
""",
    "init": f"""{BOLD}bluei init{RESET}

{DIM}Register any git project with bluei. Auto-discovers git remote and
language, then registers in the local registry.{RESET}

{BOLD}Usage:{RESET}
  bluei init [{YELLOW}--path {CYAN}<dir>{RESET}] [{YELLOW}--name {CYAN}<name>{RESET}]

{BOLD}Options:{RESET}
  --path, -p {CYAN}<dir>{RESET}        Path to a git project (default: current directory)
  --name, -n {CYAN}<name>{RESET}       Custom name (default: directory name)

{DIM}Without --path, launches the interactive first-run wizard.{RESET}

{BOLD}Examples:{RESET}
  bluei init                      {DIM}# Interactive wizard{RESET}
  bluei init --path ~/my-project  {DIM}# Register a specific project{RESET}
  bluei init -p . -n my-app       {DIM}# Register cwd with custom name{RESET}
""",
    "onboard": f"""{BOLD}bluei onboard{RESET}

{DIM}Headless onboarding for an existing local project.{RESET}

{BOLD}Usage:{RESET}
  bluei onboard --repo {CYAN}<path>{RESET} [options]

{BOLD}Options:{RESET}
  --name {CYAN}<name>{RESET}              Custom name (default: directory name)
  --language {CYAN}<lang>{RESET}          Force language detection
  --mode {CYAN}<mode>{RESET}              Care level: watch-only, note-only, offer-fixes, full-care
  --profile {CYAN}<profile>{RESET}        Care style: conservative, balanced, aggressive
  --skip-baseline            Skip starting point capture
  --allow-dirty-worktree     Allow onboarding with dirty working tree
""",
    "install": f"""{BOLD}bluei install{RESET}

{DIM}Print installation/setup instructions.{RESET}

{BOLD}One-command install (recommended):{RESET}
  curl -fsSL https://bluei.dev/install.sh | bash

{BOLD}Manual install:{RESET}
  git clone https://github.com/waterworkshq/bluei ~/.bluei
  cd ~/.bluei && ./scripts/bootstrap.sh
  ln -sf "$(pwd)/bin/bluei" ~/.local/bin/bluei

{BOLD}Verify:{RESET}
  bluei --version
  bluei help
""",
    "status": f"""{BOLD}bluei status{RESET} [{CYAN}<project-name>{RESET}]

{DIM}Show agent status or project status.{RESET}

{BOLD}Usage:{RESET}
  bluei status                 {DIM}# Show agent-wide status{RESET}
  bluei status {CYAN}<name>{RESET}      {DIM}# Show specific project status{RESET}
""",
    "scan": f"""{BOLD}bluei scan{RESET} {CYAN}<project-name>{RESET} [{YELLOW}options{RESET}]

{DIM}Run a discovery/issue-cycle scan on a project.
Discovers specks and creates GitHub issues for them.{RESET}

{BOLD}Usage:{RESET}
  bluei scan {CYAN}<project-name>{RESET} [{YELLOW}--fix-engine {CYAN}<engine>{RESET}]

{BOLD}Options:{RESET}
  --fix-engine {CYAN}<engine>{RESET}     Fix engine: auto, deterministic, claude, opencode
""",
    "clean": f"""{BOLD}bluei clean{RESET} {CYAN}<project-name>{RESET} [{YELLOW}options{RESET}]

{DIM}Fix specks and open pull requests.
Creates issues AND opens PRs with automated fixes.{RESET}

{BOLD}Usage:{RESET}
  bluei clean {CYAN}<project-name>{RESET} [{YELLOW}--fix-engine {CYAN}<engine>{RESET}]

{BOLD}Options:{RESET}
  --fix-engine {CYAN}<engine>{RESET}     Fix engine: auto, deterministic, claude, opencode
""",
    "duster": f"""{BOLD}bluei duster{RESET} {CYAN}<project-name>{RESET}

{DIM}A duster is a gentle, non-destructive way to remove dust.
Scans the project for specks and reports what it finds without
creating any issues, PRs, or making any changes.{RESET}

{BOLD}Usage:{RESET}
  bluei duster {CYAN}<project-name>{RESET}
""",
    "doctor": f"""{BOLD}bluei doctor{RESET} [{CYAN}<name>{RESET}]

{DIM}Run operational diagnostics on a project or the whole agent.
Checks configuration, dependencies, tool availability, and more.{RESET}

{BOLD}Usage:{RESET}
  bluei doctor {CYAN}<name>{RESET}     {DIM}# Check a specific project{RESET}
  bluei doctor                {DIM}# Check all projects{RESET}
""",
    "run": f"""{BOLD}bluei run{RESET} {CYAN}<project-name>{RESET} [{YELLOW}options{RESET}]

{DIM}Run a full orchestrated sweep (discover, fix, review, merge).
Equivalent to running scan → clean → verify in sequence.{RESET}

{BOLD}Usage:{RESET}
  bluei run {CYAN}<project-name>{RESET} [{YELLOW}--dry-run{RESET}]

{BOLD}Options:{RESET}
  --dry-run                     Preview without making changes
  --fix-engine {CYAN}<engine>{RESET}     Fix engine: auto, deterministic, claude, opencode
""",
    "secrets": f"""{BOLD}bluei secrets{RESET} {CYAN}<path>{RESET}

{DIM}Scan a directory for hardcoded secrets (API keys, tokens,
passwords, private keys, high-entropy strings).{RESET}

{BOLD}Usage:{RESET}
  bluei secrets {CYAN}<path>{RESET}
""",
    "health": f"""{BOLD}bluei health{RESET} {CYAN}<project-name>{RESET} [{YELLOW}options{RESET}]

{DIM}Show the vitality score (0–100) for a project.{RESET}

{BOLD}Usage:{RESET}
  bluei health {CYAN}<project-name>{RESET}

{BOLD}Options:{RESET}
  --days {CYAN}<n>{RESET}               Days of history (default: 30)
  --format {CYAN}<fmt>{RESET}           Output format: text, whatsapp (default: text)
""",
    "report": f"""{BOLD}bluei report{RESET} {CYAN}<project-name>{RESET} [{YELLOW}options{RESET}]

{DIM}Generate a comprehensive vitality report for a project.
PDF by default. Also supports text, JSON, HTML, and WhatsApp formats.{RESET}

{BOLD}Usage:{RESET}
  bluei report {CYAN}<project-name>{RESET}
  bluei report {CYAN}<project-name>{RESET} --format text
  bluei report {CYAN}<project-name>{RESET} -o report.pdf

{BOLD}Options:{RESET}
  --format {CYAN}<fmt>{RESET}           Output format: pdf, text, json, html, whatsapp
  --output, -o {CYAN}<path>{RESET}      Output file path
  --days {CYAN}<n>{RESET}               Days of history (default: 30)
""",
    "dashboard": f"""{BOLD}bluei dashboard{RESET} [{YELLOW}options{RESET}]

{DIM}Generate a read-only, self-contained observability dashboard from repo state.{RESET}

{BOLD}Usage:{RESET}
  bluei dashboard
  bluei dashboard --repo {CYAN}<name>{RESET}
  bluei dashboard --format json --output {CYAN}<path>{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}             Limit dashboard data to one repo
  --format {CYAN}<fmt>{RESET}            Output format: html, json (default: html)
  --output, -o {CYAN}<path>{RESET}       Output file path
  --state-root {CYAN}<dir>{RESET}        Override repos state root for tests/tools
  --open                       Open the dashboard in the default browser after writing
""",
    "languages": f"""{BOLD}bluei languages{RESET}

{DIM}List installed language packs and their declared detector rules.{RESET}

{BOLD}Usage:{RESET}
  bluei languages
""",
    "preflight": f"""{BOLD}bluei preflight{RESET} --repo {CYAN}<path>{RESET}

{DIM}Assess whether a project is ready for bluei registration.{RESET}

{BOLD}Usage:{RESET}
  bluei preflight --repo {CYAN}<path>{RESET}
""",
    "heal": f"""{BOLD}bluei heal{RESET} --repo {CYAN}<path>{RESET} [{YELLOW}options{RESET}]

{DIM}Heal a dirty worktree caused by transient artifacts (pycache, lock files).
By default runs as a dry-run — add --no-dry-run to actually heal.{RESET}

{BOLD}Usage:{RESET}
  bluei heal --repo {CYAN}<path>{RESET}             {DIM}# Dry-run preview{RESET}
  bluei heal --repo {CYAN}<path>{RESET} --no-dry-run {DIM}# Execute healing{RESET}

{BOLD}Options:{RESET}
  --no-dry-run                  Actually heal (default: dry-run)
  --remove-artifacts            Also remove transient artifact directory/files
""",
    "install-cron": f"""{BOLD}bluei install-cron{RESET} {CYAN}<project-name>{RESET} [{YELLOW}options{RESET}]

{DIM}Install host cron schedule for a project cycle automation.{RESET}

{BOLD}Usage:{RESET}
  bluei install-cron {CYAN}<project-name>{RESET}
  bluei install-cron {CYAN}<project-name>{RESET} --issue-schedule "0 */2 * * *"

{BOLD}Options:{RESET}
  --issue-schedule {CYAN}<cron>{RESET}    Issue cycle schedule (default: 0 */4 * * *)
  --pr-schedule {CYAN}<cron>{RESET}       PR cycle schedule (default: 0 */6 * * *)
  --review-schedule {CYAN}<cron>{RESET}   Review cycle schedule (default: 30 * * * *)
  --merge-schedule {CYAN}<cron>{RESET}    Merge cycle schedule (default: 0 6,18 * * *)
""",
    "ci": f"""{BOLD}bluei ci{RESET} {CYAN}<project-path>{RESET}

{DIM}Generate a GitHub Actions CI workflow (.github/workflows/bluei.yml).{RESET}

{BOLD}Usage:{RESET}
  bluei ci {CYAN}<project-path>{RESET}
  bluei ci {CYAN}<project-path>{RESET} --force      {DIM}# Overwrite existing{RESET}
""",
    "install-hook": f"""{BOLD}bluei install-hook{RESET} {CYAN}<project-path>{RESET}

{DIM}Install a pre-commit hook that runs bluei duster on every commit.{RESET}

{BOLD}Usage:{RESET}
  bluei install-hook {CYAN}<project-path>{RESET}
  bluei install-hook {CYAN}<project-path>{RESET} --force  {DIM}# Overwrite existing{RESET}
""",
    "update": f"""{BOLD}bluei update{RESET}

{DIM}Self-update to the latest version.{RESET}

{BOLD}Usage:{RESET}
  bluei update
""",
    "patterns": f"""{BOLD}bluei patterns{RESET} {CYAN}<subcommand>{RESET} [{YELLOW}options{RESET}]

{DIM}Inspect and manage learned fix patterns.{RESET}

{BOLD}Usage:{RESET}
  bluei patterns list --repo {CYAN}<name>{RESET}             {DIM}List active patterns{RESET}
  bluei patterns show {CYAN}<pattern_id>{RESET} --repo {CYAN}<name>{RESET}   {DIM}Show full pattern details{RESET}
  bluei patterns deactivate {CYAN}<pattern_id>{RESET} --repo {CYAN}<name>{RESET}  {DIM}Deactivate a pattern{RESET}
  bluei patterns reactivate {CYAN}<pattern_id>{RESET} --repo {CYAN}<name>{RESET}  {DIM}Reactivate a deactivated pattern{RESET}

{BOLD}Subcommands:{RESET}
  list                  {DIM}List all active patterns with confidence and stats{RESET}
  show {CYAN}<id>{RESET}            {DIM}Show full details including diff patch{RESET}
  deactivate {CYAN}<id>{RESET}      {DIM}Manually deactivate a pattern (set confidence to 0){RESET}
  reactivate {CYAN}<id>{RESET}      {DIM}Reactivate a deactivated pattern (set confidence to 0.5){RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}            Project name (required)

{BOLD}Examples:{RESET}
  bluei patterns list --repo my-app
  bluei patterns show fp-a1b2c3d4e5f6 --repo my-app
  bluei patterns deactivate fp-a1b2c3d4e5f6 --repo my-app
""",
    "campaign": f"""{BOLD}bluei campaign{RESET} {CYAN}<subcommand>{RESET} [{YELLOW}options{RESET}]

{DIM}Plan, inspect, dry-run, and guardedly execute multi-finding refactor campaigns.{RESET}

{BOLD}Usage:{RESET}
  bluei campaign plan --repo {CYAN}<name>{RESET} --rules {CYAN}<rules>{RESET} --paths {CYAN}<glob>{RESET}
  bluei campaign list --repo {CYAN}<name>{RESET}
  bluei campaign status {CYAN}<campaign_id>{RESET} --repo {CYAN}<name>{RESET}
  bluei campaign events {CYAN}<campaign_id>{RESET} --repo {CYAN}<name>{RESET} [--limit {CYAN}<n>{RESET}]
  bluei campaign run {CYAN}<campaign_id>{RESET} --repo {CYAN}<name>{RESET} --dry-run
  bluei campaign pause {CYAN}<campaign_id>{RESET} --repo {CYAN}<name>{RESET} [--reason {CYAN}<text>{RESET}]
  bluei campaign resume {CYAN}<campaign_id>{RESET} --repo {CYAN}<name>{RESET}
  bluei campaign abort {CYAN}<campaign_id>{RESET} --repo {CYAN}<name>{RESET} [--reason {CYAN}<text>{RESET}]

{BOLD}Subcommands:{RESET}
  plan                  {DIM}Read current findings and print an ordered campaign plan{RESET}
  list                  {DIM}List saved campaign plans{RESET}
  status                {DIM}Show a saved campaign plan{RESET}
  events                {DIM}Show recent campaign event history{RESET}
  run                   {DIM}Walk or guardedly execute a saved plan{RESET}
  pause                 {DIM}Pause a saved campaign plan{RESET}
  resume                {DIM}Resume a paused campaign plan{RESET}
  abort                 {DIM}Abort a saved campaign plan{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}             Repository name
  --rules {CYAN}<a,b,c>{RESET}           Comma-separated rule IDs to include
  --paths {CYAN}<glob[,glob]>{RESET}     Comma-separated path globs to include
  --save                       Persist a generated plan to state/campaigns
  --dry-run                    Required for campaign run in this slice
  --allow-mutate               Allow real campaign fix execution
  --worktree {CYAN}<dir>{RESET}         Worktree path required with --allow-mutate
  --reason {CYAN}<text>{RESET}          Operator reason for pause/abort
  --limit {CYAN}<n>{RESET}              Number of recent events to show
  --state-root {CYAN}<dir>{RESET}        Override repos state root for tests/tools

{BOLD}Examples:{RESET}
  bluei campaign plan --repo my-app --rules type-explicit-any --paths "src/api/**"
""",
    "emergent": f"""{BOLD}bluei emergent{RESET} {CYAN}<subcommand>{RESET} [{YELLOW}options{RESET}]

{DIM}Observe repeated findings and record proposed emergent rules in shadow/proposal state.{RESET}

{BOLD}Usage:{RESET}
  bluei emergent propose --repo {CYAN}<name>{RESET} [--min-observations {CYAN}<n>{RESET}] [--run-id {CYAN}<id>{RESET}]
  bluei emergent validate --repo {CYAN}<name>{RESET}
  bluei emergent scan --repo {CYAN}<name>{RESET} --worktree {CYAN}<path>{RESET}
  bluei emergent discover --repo {CYAN}<name>{RESET} --worktree {CYAN}<path>{RESET}
  bluei emergent shadow {CYAN}<rule_id>{RESET} --repo {CYAN}<name>{RESET} --matches {CYAN}<n>{RESET} --false-positives {CYAN}<n>{RESET}
  bluei emergent approve {CYAN}<rule_id>{RESET} --repo {CYAN}<name>{RESET}
  bluei emergent reject {CYAN}<rule_id>{RESET} --repo {CYAN}<name>{RESET} [--reason {CYAN}<text>{RESET}]
  bluei emergent retire {CYAN}<rule_id>{RESET} --repo {CYAN}<name>{RESET} [--reason {CYAN}<text>{RESET}]
  bluei emergent promote {CYAN}<rule_id>{RESET} --repo {CYAN}<name>{RESET} [--plugin-dir {CYAN}<dir>{RESET}]
  bluei emergent gc --repo {CYAN}<name>{RESET}
  bluei emergent list --repo {CYAN}<name>{RESET}
  bluei emergent show {CYAN}<rule_id>{RESET} --repo {CYAN}<name>{RESET}

{BOLD}Subcommands:{RESET}
  propose               {DIM}Create/update proposed rules from current findings{RESET}
  validate              {DIM}Promote safe proposals to candidates{RESET}
  scan                  {DIM}Run candidate rules in shadow mode against a worktree{RESET}
  discover              {DIM}Write findings from active emergent rules{RESET}
  shadow                {DIM}Record manual shadow-mode evidence for a candidate{RESET}
  approve               {DIM}Manually approve a rule to active status{RESET}
  reject                {DIM}Reject a rule proposal or candidate{RESET}
  retire                {DIM}Retire a rule without deleting its evidence{RESET}
  promote               {DIM}Retire an emergent rule and write it as hand-authored{RESET}
  gc                    {DIM}Retire stale rules (old active or proposed){RESET}
  list                  {DIM}List proposed emergent rules{RESET}
  show                  {DIM}Show proposed rule details{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}             Repository name
  --min-observations {CYAN}<n>{RESET}    Minimum repeated findings before proposal
  --from-patterns              Propose from successful fix patterns instead of findings
  --min-success-count {CYAN}<n>{RESET}   Minimum fix pattern successes before proposal
  --run-id {CYAN}<id>{RESET}             Evidence run identifier
  --worktree {CYAN}<path>{RESET}         Worktree path for shadow scanning
  --reason {CYAN}<text>{RESET}           Lifecycle transition reason
  --matches {CYAN}<n>{RESET}             Shadow matches observed
  --false-positives {CYAN}<n>{RESET}     Shadow matches judged false-positive
  --min-shadow-runs {CYAN}<n>{RESET}     Shadow runs before active/rejected decision
  --plugin-dir {CYAN}<dir>{RESET}        Plugin directory for promote (writes plugin.yaml)
  --state-root {CYAN}<dir>{RESET}        Override repos state root for tests/tools
""",
    "lesson": f"""{BOLD}bluei lesson{RESET} {CYAN}<subcommand>{RESET} [{YELLOW}options{RESET}]

{DIM}Inspect and manage per-repo lesson entries that feed the fix feedback loop.{RESET}

{BOLD}Usage:{RESET}
  bluei lesson add --repo {CYAN}<name>{RESET} [--broke {CYAN}"..."{RESET}] [--changed {CYAN}"..."{RESET}] [--worked {CYAN}"..."{RESET}] [--finding-id {CYAN}<id>{RESET}]
  bluei lesson list --repo {CYAN}<name>{RESET} [--rule {CYAN}<rule>{RESET}] [--limit {CYAN}<n>{RESET}]
  bluei lesson show {CYAN}<finding_id>{RESET} --repo {CYAN}<name>{RESET}

{BOLD}Subcommands:{RESET}
  add                   {DIM}Write a manual lesson entry (cross-finding hint){RESET}
  list                  {DIM}List recent lessons, optionally filtered by rule{RESET}
  show                  {DIM}Show lesson entries for a specific finding_id{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}             Repository name (required)
  --broke {CYAN}"..."{RESET}            What broke or failed
  --changed {CYAN}"..."{RESET}          What was changed successfully
  --worked {CYAN}"..."{RESET}           What worked or was accomplished
  --finding-id {CYAN}<id>{RESET}        Tag entry with a specific finding_id
  --rule {CYAN}<rule>{RESET}            Filter by rule name (for list)
  --limit {CYAN}<n>{RESET}              Number of entries to show (default 20)

{BOLD}Examples:{RESET}
  bluei lesson add --repo my-app --broke "ruff-b904 bare raises need from AppError(...)"
  bluei lesson add --repo my-app --changed "disable xo-complexity for legacy files"
  bluei lesson list --repo my-app --rule ruff-b904
   bluei lesson show abc123def --repo my-app
""",
    "upgrade-config": f"""{BOLD}bluei upgrade-config{RESET} {CYAN}--repo <name>{RESET}

{DIM}Upgrade an onboarded project's configuration to the latest schema version.{RESET}

{BOLD}Usage:{RESET}
  bluei upgrade-config {CYAN}--repo <name>{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}     Name of the onboarded project to upgrade.

{BOLD}Examples:{RESET}
  bluei upgrade-config --repo my-project
""",
    "notify": f"""{BOLD}bluei notify{RESET}

{DIM}Deliver escalation notifications and health digests to configured channels.{RESET}

{BOLD}Usage:{RESET}
  bluei notify {CYAN}<subcommand>{RESET} [{YELLOW}options{RESET}]

{BOLD}Subcommands:{RESET}
  test              {DIM}Send a test notification to all configured channels{RESET}
  config            {DIM}Show resolved notification configuration{RESET}
  digest            {DIM}Generate and deliver a health digest{RESET}
  log               {DIM}Show recent notification delivery history{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}          Repository name (required for test, digest, log)
  --all                     {DIM}Operate on all repos (for digest, log){RESET}
  --limit {CYAN}<n>{RESET}           Number of log entries to show (default 20)
  --global                  {DIM}Show global config only (for config){RESET}

{BOLD}Examples:{RESET}
  bluei notify test --repo my-project     {DIM}# Test all channels{RESET}
  bluei notify config --repo my-project   {DIM}# Show merged config{RESET}
  bluei notify config --global            {DIM}# Show global config{RESET}
  bluei notify digest --repo my-project   {DIM}# Send digest now{RESET}
  bluei notify digest --all               {DIM}# Send digests for all repos{RESET}
  bluei notify log --repo my-project      {DIM}# Recent deliveries{RESET}
""",
}


# ── Command dispatch ───────────────────────────────────────────
def _run_engine(args: list[str]) -> int:
    """Run the engine with given args, returns exit code."""
    python = _ensure_python()
    cmd = [python, "-m", ENGINE_MODULE] + args
    result = subprocess.run(cmd)
    return result.returncode


def _cmd_version():
    """Print version info — bluei + backend."""
    try:
        python = _ensure_python()
        backend = subprocess.run(
            [python, "-m", ENGINE_MODULE, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        backend_ver = backend.stdout.strip()
    except Exception:
        backend_ver = None

    print(f"bluei version {BLUEI_VERSION}")
    if backend_ver:
        print(f"backend: {backend_ver}")
    return 0


def _cmd_help(cmd: str | None = None):
    """Print help text."""
    if cmd and cmd in HELP_TEXT:
        print(HELP_TEXT[cmd])
    else:
        print(HELP_TEXT["bluei"])
    return 0


def _help_requested(args: list[str]) -> bool:
    """Check if -h/--help is anywhere in remaining args."""
    return any(a in ("-h", "--help") for a in args)


def _requires_name(cmd: str, rest: list[str]) -> bool:
    """Validate that a positional name was provided; return error if not."""
    if not rest or rest[0].startswith("-"):
        print(
            f"bluei: '{cmd}' needs a project name. Try 'bluei {cmd} <project>'.",
            file=sys.stderr,
        )
        return False
    return True


# ── Subcommand handlers ────────────────────────────────────────
def _parse_init_args(rest: List[str]) -> tuple:
    """Parse --path and --name from init arguments."""
    path = None
    name = None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("--path", "-p"):
            i += 1
            if i < len(rest) and not rest[i].startswith("-"):
                path = rest[i]
        elif arg in ("--name", "-n"):
            i += 1
            if i < len(rest) and not rest[i].startswith("-"):
                name = rest[i]
        i += 1
    return path, name


def _cmd_init(rest: Optional[List[str]] = None):
    """Initialize a project: quick auto-register with --path, or interactive wizard."""
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry
    from bluei.app.onboarding import detect_git_remote, detect_language

    path, name = _parse_init_args(rest or [])

    if path:
        # ── Quick auto-registration flow ──
        repo_path = Path(path).expanduser().resolve()
        if not repo_path.is_dir():
            print(f"bluei: path not found: {repo_path}", file=sys.stderr)
            return 1

        # Auto-detect git remote
        remote = detect_git_remote(repo_path)
        repo_name = name or remote.get("name") or repo_path.name

        print(f"→ Detecting project at: {repo_path}")
        if remote["detected"] == "true":
            print(f"  Git remote: {remote['url']}")
            print(f"  Platform:   {remote['platform'] or 'unknown'}")
            print(f"  Owner:      {remote.get('owner', '')}")
        else:
            print("  No git remote detected (local-only project)")

        # Auto-detect language
        lang = detect_language(repo_path)
        print(f"  Language:   {lang}")

        # Prompt for care level (default: watch-only)
        default_mode = "watch-only"
        mode = os.environ.get("BLUEI_MODE", "").lower()
        if mode not in (
            "observe",
            "issue-only",
            "pr",
            "merge",
            "watch-only",
            "note-only",
            "offer-fixes",
            "full-care",
        ):
            print()
            print("Care level (how autonomous should bluei be?):")
            print("  1) Watch only    — dry-run only (default)")
            print("  2) Note only     — create issues, no PRs")
            print("  3) Offer fixes   — issues + PRs, no merges")
            print("  4) Full care     — full autonomy")
            choice = input("Choice [1/2/3/4, default=1]: ").strip()
            mode_map = {
                "1": "watch-only",
                "2": "note-only",
                "3": "offer-fixes",
                "4": "full-care",
            }
            mode = mode_map.get(choice, "watch-only")

        print(f"→ Care level: {mode}")

        # Register in registry
        config = ConfigManager()
        registry = RepoRegistry(config)

        from bluei.app.models import SafetyMode

        internal_mode = SafetyMode.from_brand(mode).value

        try:
            registry.create_from_path(repo_path, name=repo_name, mode=internal_mode)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            return 1

        print()
        print(f"  Project '{repo_name}' registered.")
        print(f"   Language:     {lang}")
        print(f"   Care level:   {mode}")
        print()
        print(f"   Run 'bluei scan {repo_name}' when ready.")
        return 0

    # ── Fall through to interactive wizard ──
    return _run_engine(["init"])


def _cmd_onboard(rest: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.health import HealthEngine
    from bluei.app.onboarding import OnboardEngine, OnboardOptions
    from bluei.app.plugins import PluginLoader
    from bluei.app.registry import RepoRegistry
    from bluei.app.state import StateManager

    repo_path = _parse_repo_arg(rest) or _parse_option(rest, "--path")
    if not repo_path:
        print("bluei: onboard requires --repo <path>.", file=sys.stderr)
        return 1

    workspace = Path(BLUEI_DIR)
    config_mgr = ConfigManager(workspace)
    engine = OnboardEngine(
        RepoRegistry(config_mgr),
        PluginLoader(workspace / "plugins"),
        HealthEngine(),
        StateManager(config_mgr.repos_dir),
    )
    options = OnboardOptions(
        name=_parse_option(rest, "--name"),
        language=_parse_option(rest, "--language"),
        framework=_parse_option(rest, "--framework"),
        plugin_id=_parse_option(rest, "--plugin"),
        template=_parse_option(rest, "--template"),
        mode=_parse_option(rest, "--mode") or "observe",
        profile=_parse_option(rest, "--profile") or "conservative",
        allow_dirty_worktree=_has_flag(rest, "--allow-dirty-worktree"),
    )
    try:
        result = engine.onboard(Path(repo_path), options)
    except ValueError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1

    print(f"Onboarded {result.repo.config.name}")
    print(f"Language: {result.language.name}")
    print(f"Findings: {result.findings_count}")
    return 0


def _cmd_upgrade_config(rest: list[str]) -> int:
    from bluei.app.onboarding import OnboardEngine
    from bluei.app.registry import RepoRegistry
    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager
    from bluei.app.plugins import PluginLoader
    from bluei.app.health import HealthEngine

    workspace = Path(BLUEI_DIR)
    config_mgr = ConfigManager(workspace)
    registry = RepoRegistry(config_mgr)
    plugin_loader = PluginLoader(workspace / "plugins")
    state = StateManager(config_mgr.repos_dir)
    engine = OnboardEngine(registry, plugin_loader, HealthEngine(), state)
    repo = _parse_repo_arg(rest)
    if not repo:
        print("bluei: upgrade-config requires --repo <name>.", file=sys.stderr)
        return 1
    result = engine.upgrade_config(repo)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_install():
    print(HELP_TEXT["install"])
    return 0


def _cmd_update():
    return _run_engine(["update"])


def _cmd_status(rest: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.registry import RepoRegistry

    registry = RepoRegistry(ConfigManager())
    repos = registry.list_all()
    print("Repositories:")
    if rest and not rest[0].startswith("-"):
        repo = registry.find_by_name(rest[0])
        if not repo:
            print(f"bluei: project not found: {rest[0]}", file=sys.stderr)
            return 1
        print(
            f"{repo.config.name}: {repo.status.value} vitality={repo.current_health_score:.1f}"
        )
        return 0
    if not repos:
        print("bluei: no projects registered")
        return 0
    for repo in repos:
        print(
            f"{repo.config.name}: {repo.status.value} vitality={repo.current_health_score:.1f}"
        )
    return 0


def _cmd_scan(name: str, passthrough: list[str]):
    return _cmd_run(name, ["--phase", "issue-cycle", "--no-dry-run"] + passthrough)


def _cmd_clean(name: str, passthrough: list[str]):
    return _cmd_run(name, ["--phase", "pr-cycle", "--no-dry-run"] + passthrough)


def _cmd_duster(name: str):
    return _cmd_run(name, ["--phase", "issue-cycle", "--dry-run"])


def _cmd_doctor(rest: list[str]):
    checks = [
        ("workspace", BLUEI_DIR.exists(), str(BLUEI_DIR)),
        ("python", bool(_ensure_python()), _ensure_python()),
        ("plugins", (BLUEI_DIR / "plugins").exists(), str(BLUEI_DIR / "plugins")),
        ("templates", (BLUEI_DIR / "templates").exists(), str(BLUEI_DIR / "templates")),
    ]

    from pathlib import Path
    from bluei.engine.notify import load_notification_config

    notif_config = load_notification_config(None, Path.cwd())
    notif_enabled = notif_config.get("enabled", False)
    notif_channels = notif_config.get("channels", [])
    has_channels = len(notif_channels) > 0

    if notif_enabled:
        checks.append(
            (
                "notifications",
                has_channels,
                f"enabled, {len(notif_channels)} channel(s)",
            )
        )
        from bluei.engine.notifiers import CHANNEL_REGISTRY

        for i, ch in enumerate(notif_channels):
            ch_type = ch.get("type", "?")
            registered = ch_type in CHANNEL_REGISTRY
            has_url = bool(ch.get("url") or ch.get("to"))
            ok = registered and has_url
            detail = f"channel[{i}] type={ch_type}" + (
                ""
                if ok
                else f" ({'unregistered' if not registered else 'missing config'})"
            )
            checks.append((f"notif-channel-{i}", ok, detail))
    else:
        checks.append(("notifications", True, "disabled (default)"))

    print("bluei doctor")
    failed = 0
    for name, ok, detail in checks:
        print(f"{'OK' if ok else 'ERR'} {name}: {detail}")
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


def _cmd_run(name: str, passthrough: list[str]):
    from bluei.app.config import ConfigManager
    from bluei.app.health import HealthEngine
    from bluei.app.registry import RepoRegistry
    from bluei.app.runner import RunEngine, RunOptions
    from bluei.app.state import StateManager

    config_mgr = ConfigManager(Path(BLUEI_DIR))
    registry = RepoRegistry(config_mgr)
    repo = registry.find_by_name(name)
    if not repo:
        print(f"bluei: project not found: {name}", file=sys.stderr)
        return 1

    phase = _parse_option(passthrough, "--phase") or "orchestrated"
    dry_run = _has_flag(passthrough, "--dry-run") or not _has_flag(
        passthrough, "--no-dry-run"
    )
    fix_engine = _parse_option(passthrough, "--fix-engine")
    engine = RunEngine(
        registry, StateManager(config_mgr.repos_dir), HealthEngine(), config_mgr
    )
    result = engine.run(
        repo, RunOptions(phase=phase, dry_run=dry_run, fix_engine=fix_engine)
    )
    if result.output:
        print(result.output, end="" if result.output.endswith("\n") else "\n")
    if result.error:
        print(result.error, file=sys.stderr)
    print(f"Findings: {result.run.findings_detected}")
    return 0 if result.success else 1


def _cmd_secrets(path: str):
    python = _ensure_python()
    return subprocess.run([python, str(BLUEI_SECRETS), path]).returncode


def _cmd_health(name: str, passthrough: list[str]):
    return _run_engine(["health", "--repo", name] + passthrough)


def _cmd_report(name: str, passthrough: list[str]):
    return _run_engine(["report", "--repo", name, "--format", "pdf"] + passthrough)


def _cmd_dashboard(rest: list[str]) -> int:
    import webbrowser

    from bluei.app.config import ConfigManager
    from bluei.app.dashboard import write_dashboard
    from bluei.app.dashboard import build_dashboard_data, render_dashboard_html

    output_format = _parse_option(rest, "--format") or "html"
    if output_format not in {"html", "json"}:
        print("bluei: dashboard --format must be html or json.", file=sys.stderr)
        return 1
    state_root = _parse_option(rest, "--state-root")
    config = ConfigManager()
    repos_dir = Path(state_root) if state_root else config.repos_dir
    output = _parse_option(rest, "--output") or _parse_option(rest, "-o")
    if output:
        output_path = Path(output)
    else:
        suffix = "json" if output_format == "json" else "html"
        output_path = config.workspace / f"bluei-dashboard.{suffix}"
    try:
        written = write_dashboard(
            repos_dir=repos_dir,
            output_path=output_path,
            output_format=output_format,
            repo_name=_parse_repo_arg(rest),
        )
    except ValueError as exc:
        print(f"bluei: {exc}", file=sys.stderr)
        return 1
    print(f"Dashboard written: {written}")
    if _has_flag(rest, "--open"):
        webbrowser.open(f"file://{written}")
    if _has_flag(rest, "--watch"):
        import time

        interval = 30
        for i, arg in enumerate(rest):
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
            elif arg == "--interval" and i + 1 < len(rest):
                interval = int(rest[i + 1])
        print(f"Watching with interval={interval}s. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(interval)
                write_dashboard(
                    repos_dir=repos_dir,
                    output_path=output_path,
                    output_format=output_format,
                    repo_name=_parse_repo_arg(rest),
                )
                print(f"Refreshed at {time.strftime('%H:%M:%S')}")
        except KeyboardInterrupt:
            print("Watch stopped.")
    return 0


def _cmd_languages(rest: list[str]) -> int:
    from bluei.app.plugins import PluginLoader

    loader = PluginLoader(BLUEI_DIR / "plugins")
    manifests = loader.discover()
    for manifest in sorted(manifests, key=lambda item: item.get("id", "")):
        rules = (manifest.get("discovery") or {}).get("rules") or []
        languages = ",".join(manifest.get("languages") or [])
        print(f"{manifest.get('id')} [{languages}] {len(rules)} rule(s)")
        for rule in rules:
            print(
                f"  {rule.get('id')} "
                f"category={rule.get('category', 'lint')} "
                f"confidence={float(rule.get('confidence', 0.0)):.2f} "
                f"autofix={bool(rule.get('autofix', False))}"
            )
    return 0


def _cmd_preflight(rest: list[str]):
    return _run_engine(["preflight"] + rest)


def _cmd_heal(rest: list[str]):
    return _run_engine(["heal"] + rest)


def _cmd_ci(name: str, passthrough: list[str]):
    return _run_engine(["ci", "--repo", name] + passthrough)


def _cmd_install_hook(name: str, passthrough: list[str]):
    return _run_engine(["install-hook", "--repo", name] + passthrough)


def _cmd_install_cron(name: str, passthrough: list[str]):
    return _run_engine(["install-cron", "--repo", name] + passthrough)


def _parse_repo_arg(rest: list[str]) -> str | None:
    """Extract --repo <name> from argument list."""
    i = 0
    while i < len(rest):
        if rest[i] == "--repo" and i + 1 < len(rest):
            return rest[i + 1]
        i += 1
    return None


def _parse_option(rest: list[str], name: str) -> str | None:
    i = 0
    while i < len(rest):
        if rest[i] == name and i + 1 < len(rest):
            return rest[i + 1]
        i += 1
    return None


def _parse_csv_option(rest: list[str], name: str) -> list[str]:
    value = _parse_option(rest, name)
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _has_flag(rest: list[str], name: str) -> bool:
    return name in rest


def _parse_positive_int(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _cmd_campaign(rest: list[str]) -> int:
    """Handle campaign planning commands."""
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["campaign"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in {
        "plan",
        "list",
        "status",
        "events",
        "run",
        "pause",
        "resume",
        "abort",
    }:
        print(
            f"bluei: unknown campaign subcommand '{subcmd}'. Try 'bluei help campaign'.",
            file=sys.stderr,
        )
        return 1

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            f"bluei: campaign {subcmd} requires --repo <name>. Try 'bluei help campaign'.",
            file=sys.stderr,
        )
        return 1

    from bluei.campaigns.planner import CampaignPlanner
    from bluei.campaigns.state import CampaignStateManager
    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager

    state_root = _parse_option(sub_rest, "--state-root")
    repos_dir = Path(state_root) if state_root else ConfigManager().repos_dir
    state = StateManager(repos_dir)
    campaign_store = CampaignStateManager(state.get_campaigns_dir(repo))

    if subcmd == "list":
        return _campaign_list(campaign_store)

    if subcmd == "status":
        campaign_id = _campaign_id_arg(sub_rest)
        if not campaign_id:
            print(
                "bluei: campaign status requires a campaign_id. Try 'bluei help campaign'.",
                file=sys.stderr,
            )
            return 1
        try:
            campaign = campaign_store.load(campaign_id)
        except FileNotFoundError:
            print(f"bluei: campaign '{campaign_id}' not found.", file=sys.stderr)
            return 1
        _print_campaign_status(campaign, campaign_store.last_event(campaign_id))
        return 0

    if subcmd == "events":
        campaign_id = _campaign_id_arg(sub_rest)
        if not campaign_id:
            print(
                "bluei: campaign events requires a campaign_id. Try 'bluei help campaign'.",
                file=sys.stderr,
            )
            return 1
        try:
            campaign_store.load(campaign_id)
        except FileNotFoundError:
            print(f"bluei: campaign '{campaign_id}' not found.", file=sys.stderr)
            return 1
        limit = _parse_positive_int(_parse_option(sub_rest, "--limit"), default=20)
        return _campaign_events(campaign_store, campaign_id, limit)

    if subcmd == "run":
        campaign_id = _campaign_id_arg(sub_rest)
        if not campaign_id:
            print(
                "bluei: campaign run requires a campaign_id. Try 'bluei help campaign'.",
                file=sys.stderr,
            )
            return 1
        allow_mutate = _has_flag(sub_rest, "--allow-mutate")
        worktree = _parse_option(sub_rest, "--worktree")
        if allow_mutate and not worktree:
            print(
                "bluei: campaign run --allow-mutate requires --worktree <dir>.",
                file=sys.stderr,
            )
            return 1
        if not allow_mutate and not _has_flag(sub_rest, "--dry-run"):
            print("bluei: campaign run currently requires --dry-run.", file=sys.stderr)
            return 1
        from bluei.campaigns.executor import CampaignExecutor, autofix_fix_runner

        _psp = None
        try:
            from bluei.app.state import StateManager

            _psp_file = state.get_fix_patterns_file(repo)
            if _psp_file.exists():
                _psp = _psp_file
        except Exception:
            pass
        try:
            if allow_mutate:
                result = CampaignExecutor(
                    campaign_store,
                    fix_runner=autofix_fix_runner,
                    pattern_store_path=_psp,
                ).run(
                    campaign_id,
                    dry_run=False,
                    worktree_path=Path(worktree),
                    require_clean_worktree=True,
                )
            else:
                result = CampaignExecutor(campaign_store).run(campaign_id, dry_run=True)
        except FileNotFoundError:
            print(f"bluei: campaign '{campaign_id}' not found.", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"bluei: {exc}", file=sys.stderr)
            return 1
        mode = "Run" if allow_mutate else "Dry-run"
        print(
            f"{mode} complete: campaign={result['campaign_id']} "
            f"status={getattr(result['status'], 'value', result['status'])} phases={result['phases_walked']} "
            f"fixed={result['findings_fixed']} failed={result['findings_failed']} "
            f"skipped={result['findings_skipped']}"
        )
        return 0

    if subcmd in {"pause", "resume", "abort"}:
        campaign_id = _campaign_id_arg(sub_rest)
        if not campaign_id:
            print(
                f"bluei: campaign {subcmd} requires a campaign_id. Try 'bluei help campaign'.",
                file=sys.stderr,
            )
            return 1
        reason = _parse_option(sub_rest, "--reason") or subcmd
        try:
            campaign = campaign_store.load(campaign_id)
        except FileNotFoundError:
            print(f"bluei: campaign '{campaign_id}' not found.", file=sys.stderr)
            return 1
        if subcmd == "pause":
            if campaign.status in {CampaignStatus.completed, CampaignStatus.aborted}:
                print(
                    f"bluei: cannot pause {campaign.status} campaign.", file=sys.stderr
                )
                return 1
            _transition_campaign(
                campaign_store,
                campaign,
                CampaignStatus.paused,
                "campaign.paused",
                reason,
            )
            print(f"Paused: {campaign_id}")
        elif subcmd == "resume":
            if campaign.status in {CampaignStatus.completed, CampaignStatus.aborted}:
                print(
                    f"bluei: cannot resume {campaign.status} campaign.", file=sys.stderr
                )
                return 1
            _resume_campaign(campaign_store, campaign)
            print(f"Resumed: {campaign_id}")
        else:
            if campaign.status == CampaignStatus.completed:
                print("bluei: cannot abort completed campaign.", file=sys.stderr)
                return 1
            campaign.abort_reason = reason
            _transition_campaign(
                campaign_store,
                campaign,
                CampaignStatus.aborted,
                "campaign.aborted",
                reason,
            )
            print(f"Aborted: {campaign_id}")
        return 0

    findings = state.load_findings(repo)
    target_rules = _parse_csv_option(sub_rest, "--rules")
    target_paths = _parse_csv_option(sub_rest, "--paths")
    strategy = _parse_option(sub_rest, "--strategy") or "rule_based"

    campaign = CampaignPlanner().plan(
        findings,
        repo=repo,
        title=f"Campaign for {repo}",
        target_rules=target_rules,
        target_paths=target_paths,
        strategy=strategy,
    )
    _print_campaign_plan(campaign)
    if _has_flag(sub_rest, "--save") and campaign.status != CampaignStatus.aborted:
        campaign_store.save(campaign)
        campaign_store.append_event(
            campaign.campaign_id, "campaign.created", {"repo": repo}
        )
        print(f"Saved: {campaign.campaign_id}")
    return 0 if campaign.status != CampaignStatus.aborted else 2


def _campaign_id_arg(rest: list[str]) -> str | None:
    for arg in rest:
        if arg.startswith("-"):
            return None
        return arg
    return None


def _positional_arg(rest: list[str]) -> str | None:
    return _campaign_id_arg(rest)


def _campaign_list(campaign_store) -> int:
    campaigns = campaign_store.list()
    if not campaigns:
        print("No saved campaigns found.")
        return 0

    print(f"{'CAMPAIGN ID':<28} {'STATUS':<10} {'PHASES':>6} {'FINDINGS':>8} TITLE")
    print("-" * 80)
    for campaign in campaigns:
        print(
            f"{campaign.campaign_id:<28} {campaign.status:<10} "
            f"{len(campaign.phases):>6} {campaign.total_findings:>8} {campaign.title}"
        )
    return 0


def _campaign_events(campaign_store, campaign_id: str, limit: int) -> int:
    events = campaign_store.events(campaign_id, limit=limit)
    print(f"Events: {campaign_id}")
    if not events:
        print("No events recorded.")
        return 0
    for event in events:
        payload = event.get("payload") or {}
        payload_text = json.dumps(payload, sort_keys=True) if payload else "{}"
        print(f"{event.get('ts', '')} {event.get('event', '')} {payload_text}")
    return 0


def _transition_campaign(
    campaign_store, campaign, status: str, event: str, reason: str
) -> None:
    campaign.status = status
    if status == CampaignStatus.paused:
        from bluei.app.models import now_iso

        campaign.pause_reason = reason
        campaign.pause_phase_id = None
        campaign.pause_finding_id = None
        campaign.paused_at = now_iso()
    elif status in {CampaignStatus.aborted, CampaignStatus.completed}:
        campaign.pause_reason = None
        campaign.pause_phase_id = None
        campaign.pause_finding_id = None
        campaign.paused_at = None
    if status in {CampaignStatus.paused, CampaignStatus.aborted}:
        for phase in campaign.phases:
            if phase.status == PhaseStatus.active:
                phase.status = status
    campaign_store.save(campaign)
    campaign_store.append_event(
        campaign.campaign_id,
        event,
        {"reason": reason},
    )


def _resume_campaign(campaign_store, campaign) -> None:
    campaign.status = CampaignStatus.planning
    campaign.pause_reason = None
    campaign.pause_phase_id = None
    campaign.pause_finding_id = None
    campaign.paused_at = None
    for phase in campaign.phases:
        if phase.status == PhaseStatus.paused:
            phase.status = PhaseStatus.pending
    campaign_store.save(campaign)
    campaign_store.append_event(
        campaign.campaign_id,
        "campaign.resumed",
        {"reason": "resume"},
    )


def _print_campaign_plan(campaign) -> None:
    print(f"Campaign plan: {campaign.title}")
    print(f"ID: {campaign.campaign_id}")
    print(f"Repo: {campaign.repo}")
    print(f"Status: {campaign.status}")
    print(f"Findings: {campaign.total_findings}")
    if campaign.target_rules:
        print(f"Rules: {', '.join(campaign.target_rules)}")
    if campaign.target_paths:
        print(f"Paths: {', '.join(campaign.target_paths)}")
    if campaign.abort_reason:
        print(f"Abort reason: {campaign.abort_reason}")
        return
    if not campaign.phases:
        print("No matching findings.")
        return
    print()
    for phase in campaign.phases:
        deps = f" depends_on={','.join(phase.depends_on)}" if phase.depends_on else ""
        print(
            f"Phase {phase.index + 1}: {phase.title} "
            f"({len(phase.finding_ids)} finding(s), {phase.execution_mode}){deps}"
        )


def _print_campaign_status(campaign, last_event: dict | None = None) -> None:
    print(f"Campaign: {campaign.campaign_id}")
    print(f"Title: {campaign.title}")
    print(f"Repo: {campaign.repo}")
    print(f"Status: {campaign.status}")
    print(f"Findings: {campaign.total_findings}")
    print(f"Phases: {len(campaign.phases)}")
    print(
        "Progress: "
        f"fixed={campaign.findings_fixed} "
        f"failed={campaign.findings_failed} "
        f"skipped={campaign.findings_skipped}"
    )
    if campaign.pause_reason:
        print(f"Pause reason: {campaign.pause_reason}")
    if campaign.pause_phase_id:
        print(f"Pause phase: {campaign.pause_phase_id}")
    if campaign.pause_finding_id:
        print(f"Pause finding: {campaign.pause_finding_id}")
    if last_event:
        print(f"Last event: {last_event.get('event')}")
    if campaign.abort_reason:
        print(f"Abort reason: {campaign.abort_reason}")
    if campaign.phases:
        print()
    for phase in campaign.phases:
        deps = f" depends_on={','.join(phase.depends_on)}" if phase.depends_on else ""
        print(
            f"Phase {phase.index + 1}: {phase.title} "
            f"[{phase.status}] ({len(phase.finding_ids)} finding(s), {phase.execution_mode}; "
            f"fixed={phase.findings_fixed} failed={phase.findings_failed} "
            f"skipped={phase.findings_skipped}){deps}"
        )


def _cmd_emergent(rest: list[str]) -> int:
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["emergent"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in {
        "propose",
        "validate",
        "scan",
        "discover",
        "shadow",
        "reject",
        "retire",
        "approve",
        "promote",
        "gc",
        "list",
        "show",
    }:
        print(
            f"bluei: unknown emergent subcommand '{subcmd}'. Try 'bluei help emergent'.",
            file=sys.stderr,
        )
        return 1

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            f"bluei: emergent {subcmd} requires --repo <name>. Try 'bluei help emergent'.",
            file=sys.stderr,
        )
        return 1

    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager
    from bluei.app.emergent_rules import EmergentRuleStore

    state_root = _parse_option(sub_rest, "--state-root")
    repos_dir = Path(state_root) if state_root else ConfigManager().repos_dir
    state = StateManager(repos_dir)
    store = EmergentRuleStore(state.get_emergent_rules_file(repo))

    if subcmd == "propose":
        run_id = _parse_option(sub_rest, "--run-id") or "manual"
        if _has_flag(sub_rest, "--from-patterns"):
            from bluei.engine.pattern_store import FixPatternStore

            patterns_file = state.get_fix_patterns_file(repo)
            patterns = FixPatternStore(patterns_file).load_active()
            result = store.observe_fix_patterns(
                patterns,
                run_id=run_id,
                min_success_count=_parse_positive_int(
                    _parse_option(sub_rest, "--min-success-count"),
                    default=5,
                ),
            )
        else:
            findings = state.load_findings(repo)
            min_observations = _parse_positive_int(
                _parse_option(sub_rest, "--min-observations"),
                default=5,
            )
            result = store.observe_findings(
                findings,
                run_id=run_id,
                min_observations=min_observations,
                existing_rule_ids=_detector_catalog_rule_ids(),
            )
        print(
            "Emergent proposals: "
            f"created={result['created']} updated={result['updated']} skipped={result['skipped']}"
        )
        return 0

    if subcmd == "validate":
        result = store.validate_proposals(
            existing_rule_ids=_detector_catalog_rule_ids()
        )
        print(
            "Emergent validation: "
            f"candidate={result['candidate']} rejected={result['rejected']} unchanged={result['unchanged']}"
        )
        return 0

    if subcmd == "scan":
        worktree = _parse_option(sub_rest, "--worktree")
        if not worktree:
            print(
                "bluei: emergent scan requires --worktree <path>. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        from bluei.app.emergent_rules import scan_shadow_rules

        matches = scan_shadow_rules(store.load(), Path(worktree))
        print(f"Emergent scan: matches={len(matches)}")
        for match in matches:
            print(f"{match.rule_id} {match.path}:{match.line} {match.snippet}")
        return 0

    if subcmd == "discover":
        worktree = _parse_option(sub_rest, "--worktree")
        if not worktree:
            print(
                "bluei: emergent discover requires --worktree <path>. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        from bluei.app.emergent_rules import discover_active_rule_findings

        findings = discover_active_rule_findings(
            store.load(), Path(worktree), repo_name=repo
        )
        written = state.append_findings(repo, findings)
        print(f"Emergent discover: findings={len(findings)} written={written}")
        return 0

    if subcmd == "shadow":
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                "bluei: emergent shadow requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        try:
            result = store.record_shadow_run(
                rule_id,
                matches=_parse_positive_int(
                    _parse_option(sub_rest, "--matches"), default=0
                ),
                false_positives=_parse_positive_int(
                    _parse_option(sub_rest, "--false-positives"),
                    default=0,
                ),
                min_shadow_runs=_parse_positive_int(
                    _parse_option(sub_rest, "--min-shadow-runs"),
                    default=3,
                ),
            )
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        print(
            "Emergent shadow: "
            f"rule={rule_id} status={result['status']} "
            f"shadow_runs={result.get('shadow_runs', 0)} "
            f"false_positive_rate={result.get('false_positive_rate', 0.0):.3f}"
        )
        return 0

    if subcmd == "approve":
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                "bluei: emergent approve requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        try:
            result = store.approve_rule(rule_id)
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        if not result.get("updated"):
            print(
                f"Emergent approve: rule={rule_id} status={result['status']} reason={result.get('reason', '')}"
            )
        else:
            print(f"Emergent approve: rule={rule_id} status={result['status']}")
        return 0

    if subcmd == "promote":
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                "bluei: emergent promote requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        plugin_dir_arg = _parse_option(sub_rest, "--plugin-dir")
        plugin_dir = Path(plugin_dir_arg) if plugin_dir_arg else None
        try:
            result = store.promote_rule(rule_id, plugin_dir=plugin_dir)
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    if subcmd == "gc":
        result = store.retire_stale_rules()
        print(f"Emergent gc: retired={result['retired']}")
        return 0

    if subcmd in {"reject", "retire"}:
        rule_id = _positional_arg(sub_rest)
        if not rule_id:
            print(
                f"bluei: emergent {subcmd} requires a rule_id. Try 'bluei help emergent'.",
                file=sys.stderr,
            )
            return 1
        reason = _parse_option(sub_rest, "--reason") or "manual"
        try:
            if subcmd == "reject":
                result = store.reject_rule(rule_id, reason=reason)
            else:
                result = store.retire_rule(rule_id, reason=reason)
        except KeyError:
            print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
            return 1
        print(f"Emergent {subcmd}: rule={rule_id} status={result['status']}")
        return 0

    if subcmd == "list":
        return _emergent_list(store)

    rule_id = _positional_arg(sub_rest)
    if not rule_id:
        print(
            "bluei: emergent show requires a rule_id. Try 'bluei help emergent'.",
            file=sys.stderr,
        )
        return 1
    return _emergent_show(store, rule_id)


def _detector_catalog_rule_ids() -> set[str]:
    try:
        from bluei.engine.constants import DETECTOR_CATALOG
    except Exception:
        return set()
    return {str(entry.get("rule")) for entry in DETECTOR_CATALOG if entry.get("rule")}


def _emergent_list(store) -> int:
    rules = store.load()
    if not rules:
        print("No emergent rules found.")
        return 0
    print(f"{'RULE ID':<16} {'STATUS':<10} {'OBS':>4} {'PATTERN'}")
    for rule in rules:
        pattern = rule.detection_pattern
        print(
            f"{rule.rule_id:<16} {rule.status.value:<10} "
            f"{rule.observation_count:>4} {pattern.search_pattern} in {pattern.file_glob}"
        )
    return 0


def _emergent_show(store, rule_id: str) -> int:
    for rule in store.load():
        if rule.rule_id != rule_id:
            continue
        pattern = rule.detection_pattern
        print(f"Rule: {rule.rule_id}")
        print(f"Status: {rule.status.value}")
        print(f"Header: {rule.header}")
        print(f"Pattern: {pattern.search_pattern} in {pattern.file_glob}")
        print(f"Language: {rule.language}")
        print(f"Category: {rule.category}")
        print(f"Observations: {rule.observation_count}")
        print(f"Shadow runs: {rule.shadow_runs}")
        print(f"False positive rate: {rule.false_positive_rate:.3f}")
        if rule.evidence_runs:
            print(f"Evidence runs: {', '.join(rule.evidence_runs)}")
        if rule.source_finding_ids:
            print(f"Source findings: {', '.join(rule.source_finding_ids)}")
        if rule.rejected_reason:
            print(f"Rejected reason: {rule.rejected_reason}")
        if rule.notes:
            print(f"Notes: {rule.notes}")
        return 0
    print(f"bluei: emergent rule '{rule_id}' not found.", file=sys.stderr)
    return 1


def _cmd_patterns(rest: list[str]) -> int:
    """Handle patterns subcommands: list, show, deactivate, reactivate."""
    from bluei.engine.pattern_store import FixPatternStore, DEACTIVATION_THRESHOLD
    from bluei.engine.pattern_replay import PROMPT_HINT_THRESHOLD
    from bluei.app.config import ConfigManager
    from bluei.app.state import StateManager

    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["patterns"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            "bluei: patterns requires --repo <name>. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    config = ConfigManager()
    state = StateManager(config.repos_dir)
    patterns_file = state.get_fix_patterns_file(repo)

    if not patterns_file.exists() and subcmd != "list":
        print(f"bluei: no patterns file for repo '{repo}'.", file=sys.stderr)
        return 1

    store = FixPatternStore(patterns_file)

    if subcmd == "list":
        return _patterns_list(store)
    elif subcmd == "show":
        return _patterns_show(sub_rest, store)
    elif subcmd == "deactivate":
        return _patterns_deactivate(sub_rest, store)
    elif subcmd == "reactivate":
        return _patterns_reactivate(sub_rest, store)
    else:
        print(
            f"bluei: unknown patterns subcommand '{subcmd}'. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1


def _patterns_list(store) -> int:
    from bluei.engine.pattern_store import DEACTIVATION_THRESHOLD

    patterns = store.load_active()
    if not patterns:
        print("No active patterns found.")
        return 0

    patterns.sort(key=lambda p: p.confidence, reverse=True)
    print(
        f"{'PATTERN ID':<20} {'RULE':<25} {'CONFIDENCE':>10} {'SUCC':>5} {'FAIL':>5} {'LAST USED'}"
    )
    print("-" * 85)
    for p in patterns:
        last_used = p.last_used_at[:19] if p.last_used_at else "never"
        print(
            f"{p.pattern_id:<20} {p.rule:<25} {p.confidence:>10.3f} {p.success_count:>5} {p.failure_count:>5} {last_used}"
        )
    print()
    print(f"Total: {len(patterns)} active patterns")
    return 0


def _patterns_show(rest: list[str], store) -> int:
    pattern_id = None
    for i, arg in enumerate(rest):
        if not arg.startswith("-") and i == 0:
            pattern_id = arg
            break
    if not pattern_id:
        print(
            "bluei: patterns show requires a pattern_id. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    pattern = store.get_pattern(pattern_id)
    if not pattern:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    p = pattern
    print(f"Pattern:    {p.pattern_id}")
    print(f"Rule:       {p.rule}")
    print(f"Language:   {p.language}")
    print(f"File:       {p.file_path}")
    print(f"Source:     {p.source}")
    print(f"Confidence: {p.confidence:.3f}")
    print(
        f"Success:    {p.success_count}  Fail: {p.failure_count}  Skip: {p.skip_count}"
    )
    print(f"Created:    {p.created_at[:19]}")
    print(f"Last used:  {(p.last_used_at or 'never')[:19]}")
    print(f"Last verified: {(p.last_verified_at or 'never')[:19]}")
    if p.source_finding_ids:
        print(f"Findings:   {', '.join(p.source_finding_ids[:5])}")
    print()
    print(f"{BOLD}Before:{RESET}")
    for line in p.before_snippet.splitlines():
        print(f"  {line}")
    print()
    print(f"{BOLD}After:{RESET}")
    for line in p.after_snippet.splitlines():
        print(f"  {line}")
    print()
    if p.diff_patch:
        print(f"{BOLD}Diff patch:{RESET}")
        for line in p.diff_patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(f"  {GREEN}{line}{RESET}")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"  {RED}{line}{RESET}")
            else:
                print(f"  {line}")
    return 0


def _patterns_deactivate(rest: list[str], store) -> int:
    pattern_id = None
    for i, arg in enumerate(rest):
        if not arg.startswith("-") and i == 0:
            pattern_id = arg
            break
    if not pattern_id:
        print(
            "bluei: patterns deactivate requires a pattern_id. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    pattern = store.get_pattern(pattern_id)
    if not pattern:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    delta = 0.0 - pattern.confidence
    store.update_confidence(pattern_id, delta)
    print(f"Pattern {pattern_id} deactivated (confidence set to 0.0).")
    return 0


def _patterns_reactivate(rest: list[str], store) -> int:
    from bluei.engine.pattern_replay import PROMPT_HINT_THRESHOLD

    pattern_id = None
    for i, arg in enumerate(rest):
        if not arg.startswith("-") and i == 0:
            pattern_id = arg
            break
    if not pattern_id:
        print(
            "bluei: patterns reactivate requires a pattern_id. Try 'bluei help patterns'.",
            file=sys.stderr,
        )
        return 1

    pattern = store.get_pattern(pattern_id)
    if not pattern:
        print(f"bluei: pattern '{pattern_id}' not found.", file=sys.stderr)
        return 1

    delta = PROMPT_HINT_THRESHOLD - pattern.confidence
    store.update_confidence(pattern_id, delta)
    print(
        f"Pattern {pattern_id} reactivated (confidence set to {PROMPT_HINT_THRESHOLD})."
    )
    return 0


def _cmd_lesson(rest: list[str]) -> int:
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["lesson"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in ("add", "list", "show"):
        print(
            f"bluei: unknown lesson subcommand '{subcmd}'. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            f"bluei: lesson {subcmd} requires --repo <name>. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    from bluei.app.config import ConfigManager, WORKSPACE
    from bluei.engine.constants import repo_lessons_path

    cm = ConfigManager(WORKSPACE)
    repo_config = cm.load_repo_config(repo)
    if not repo_config:
        print(f"bluei: repo '{repo}' not found in registry.", file=sys.stderr)
        return 1

    try:
        repo_path = Path(repo_config.path)
        lessons_file = repo_lessons_path(repo_path)
    except Exception as e:
        print(f"bluei: failed to resolve repo path: {e}", file=sys.stderr)
        return 1

    if subcmd == "add":
        return _lesson_add(lessons_file, sub_rest)
    elif subcmd == "list":
        return _lesson_list(lessons_file, sub_rest)
    elif subcmd == "show":
        return _lesson_show(lessons_file, sub_rest)
    return 0


def _lesson_add(lessons_file: Path, rest: list[str]) -> int:
    broke = _parse_option(rest, "--broke")
    changed = _parse_option(rest, "--changed")
    worked = _parse_option(rest, "--worked")
    finding_id = _parse_option(rest, "--finding-id")

    if not any([broke, changed, worked]):
        print(
            "bluei: lesson add requires at least one of --broke, --changed, --worked. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    from bluei.engine.utils import append_lesson

    append_lesson(
        lessons_file=lessons_file,
        cycle_type="manual",
        finding_id=finding_id or "",
        what_broke=broke or "",
        what_changed=changed or "",
        what_worked=worked or "",
    )
    print(f"Lesson added to {lessons_file}")
    return 0


def _lesson_list(lessons_file: Path, rest: list[str]) -> int:
    from bluei.engine.utils import load_lessons_for_rule

    rule = _parse_option(rest, "--rule")
    limit_str = _parse_option(rest, "--limit")
    limit = int(limit_str) if limit_str else 20

    if rule:
        entries = load_lessons_for_rule(rule, lessons_file, limit=limit)
    else:
        entries = _list_all_lessons(lessons_file, limit=limit)

    if not entries:
        filter_desc = f" for rule '{rule}'" if rule else ""
        print(f"No lesson entries found{filter_desc}.")
        return 0

    for entry in entries:
        date = entry.get("date", "?")
        cycle = entry.get("cycle_type", "?")
        fid = entry.get("finding_id", "")
        status = (
            entry.get("changed")
            or entry.get("broke")
            or entry.get("worked")
            or "(empty)"
        )
        decayed = " [decayed]" if entry.get("decayed") else ""
        print(f"{date} ({cycle}){decayed}: {status}")
        if fid:
            print(f"  finding_id: {fid}")
    return 0


def _list_all_lessons(lessons_file: Path, limit: int = 20) -> list:
    if not lessons_file.exists():
        return []
    content = lessons_file.read_text(encoding="utf-8")
    entries = [e for e in content.split("\n## ") if e.strip()]
    result = []
    for entry_text in entries[-limit:]:
        lines = entry_text.strip().split("\n")
        entry = {
            "date": "",
            "cycle_type": "",
            "finding_id": "",
            "broke": "",
            "changed": "",
            "worked": "",
            "decayed": False,
        }
        if lines:
            parts = lines[0].split("|")
            entry["date"] = parts[0].strip() if parts else ""
            entry["cycle_type"] = parts[1].strip() if len(parts) > 1 else ""
        for line in lines[1:]:
            if line.startswith("finding_id:"):
                entry["finding_id"] = line.split(":", 1)[1].strip()
            elif line.startswith("- **Broke:**"):
                entry["broke"] = line.split("**Broke:**", 1)[1].strip()
            elif line.startswith("- **Changed:**"):
                entry["changed"] = line.split("**Changed:**", 1)[1].strip()
            elif line.startswith("- **Worked:**"):
                entry["worked"] = line.split("**Worked:**", 1)[1].strip()
        result.append(entry)
    result.reverse()
    return result


def _lesson_show(lessons_file: Path, rest: list[str]) -> int:
    finding_id = None
    for arg in rest:
        if not arg.startswith("-"):
            finding_id = arg
            break
    if not finding_id:
        print(
            "bluei: lesson show requires a finding_id. Try 'bluei help lesson'.",
            file=sys.stderr,
        )
        return 1

    from bluei.engine.utils import load_lessons_for_finding

    entries = load_lessons_for_finding(finding_id, lessons_file)
    if not entries:
        print(f"No lesson entries for finding_id '{finding_id}'.")
        return 0

    print(f"{len(entries)} entries for finding_id '{finding_id}':\n")
    for entry in entries:
        date = entry.get("date", "?")
        cycle = entry.get("cycle_type", "?")
        print(f"  ## {date} | {cycle}")
        print(f"  finding_id: {entry.get('finding_id', '')}")
        broke = entry.get("broke", "")
        changed = entry.get("changed", "")
        worked = entry.get("worked", "")
        if broke:
            print(f"  - **Broke:** {broke}")
        if changed:
            print(f"  - **Changed:** {changed}")
        if worked:
            print(f"  - **Worked:** {worked}")
        print()
    return 0


def _cmd_notify(rest: list[str]) -> int:
    if not rest or rest[0] in ("-h", "--help"):
        print(HELP_TEXT["notify"])
        return 0

    subcmd = rest[0]
    sub_rest = rest[1:]
    if subcmd not in {"test", "config", "digest", "log"}:
        print(
            f"bluei: unknown notify subcommand '{subcmd}'. Try 'bluei help notify'.",
            file=sys.stderr,
        )
        return 1

    if subcmd == "test":
        return _notify_test(sub_rest)
    if subcmd == "config":
        return _notify_config(sub_rest)
    if subcmd == "digest":
        return _notify_digest(sub_rest)
    if subcmd == "log":
        return _notify_log(sub_rest)
    return 0


def _notify_test(sub_rest: list[str]) -> int:
    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            "bluei: notify test requires --repo <name>. Try 'bluei help notify'.",
            file=sys.stderr,
        )
        return 1

    from bluei.engine.notify import deliver_escalations

    test_finding = [{"type": "test", "detail": "bluei notification test"}]
    results = deliver_escalations(test_finding, repo, bypass_rate_limit=True)

    if not results:
        print("No channels configured or notifications disabled.")
        return 0

    print(f"{'CHANNEL':<12} {'STATUS':<10} {'LATENCY':<12} ERROR")
    for r in results:
        status = "OK" if r.success else "FAIL"
        latency = f"{r.latency_ms:.0f}ms" if r.latency_ms else "-"
        error = r.error or ""
        print(f"{r.channel_type:<12} {status:<10} {latency:<12} {error}")

    if any(not r.success for r in results):
        return 1
    return 0


def _notify_config(sub_rest: list[str]) -> int:
    import yaml
    from bluei.engine.notify import load_notification_config, mask_sensitive

    if _has_flag(sub_rest, "--global"):
        config = load_notification_config(None)
    else:
        repo = _parse_repo_arg(sub_rest)
        if not repo:
            print(
                "bluei: notify config requires --repo <name> or --global. Try 'bluei help notify'.",
                file=sys.stderr,
            )
            return 1
        config = load_notification_config(repo)

    display = dict(config)
    display.pop("_global", None)

    for ch in display.get("channels", []):
        if "url" in ch:
            ch["url"] = mask_sensitive(ch["url"])
        if "headers" in ch:
            for k in list(ch["headers"].keys()):
                ch["headers"][k] = mask_sensitive(str(ch["headers"][k]))

    for ch in display.get("digest", {}).get("channels", []):
        if "url" in ch:
            ch["url"] = mask_sensitive(ch["url"])

    print(yaml.dump(display, default_flow_style=False).strip())
    return 0


def _notify_digest(sub_rest: list[str]) -> int:
    from bluei.engine.notify import deliver_digest

    if _has_flag(sub_rest, "--all"):
        from bluei.app.config import ConfigManager

        cm = ConfigManager()
        repos = cm.list_repo_configs()
        any_sent = False
        for rc in repos:
            notif = rc.notifications or {}
            if notif.get("digest", {}).get("enabled"):
                results = deliver_digest(rc.name)
                sent = sum(1 for r in results if r.success)
                failed = sum(1 for r in results if not r.success)
                print(f"{rc.name}: {sent} delivered, {failed} failed")
                any_sent = True
        if not any_sent:
            print("No repos have digest.enabled=true")
        return 0

    repo = _parse_repo_arg(sub_rest)
    if not repo:
        print(
            "bluei: notify digest requires --repo <name> or --all. Try 'bluei help notify'.",
            file=sys.stderr,
        )
        return 1

    results = deliver_digest(repo)
    if not results:
        print("Digest not enabled or no channels configured.")
        return 0

    for r in results:
        status = "OK" if r.success else "FAIL"
        print(f"  {r.channel_type}: {status}")
    return 0 if all(r.success for r in results) else 1


def _notify_log(sub_rest: list[str]) -> int:
    import json
    from pathlib import Path

    repo = _parse_repo_arg(sub_rest)
    limit = _parse_positive_int(_parse_option(sub_rest, "--limit"), default=20)

    workspace = Path.cwd()
    log_path = workspace / "state" / "notification_log.jsonl"

    if not log_path.exists():
        print("No notification log found.")
        return 0

    try:
        lines = log_path.read_text().strip().splitlines()
    except OSError:
        print("Could not read notification log.", file=sys.stderr)
        return 1

    matching: list[dict] = []
    for line in lines[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if repo and entry.get("repo") != repo:
            continue
        matching.append(entry)

    print(f"{'TIMESTAMP':<28} {'REPO':<20} {'SEVERITY':<10} {'TYPE':<35} CHANNELS")
    for entry in matching[-limit:]:
        ts = entry.get("timestamp", "?")[:27]
        repo_name = entry.get("repo", "?")[:19]
        severity = entry.get("severity", "?")[:9]
        esc_type = entry.get("escalation_type", "?")[:34]
        deliveries = entry.get("deliveries", [])
        channels = ", ".join(
            f"{d.get('channel_type', '?')}:{'OK' if d.get('success') else 'FAIL'}"
            for d in deliveries
        )[:50]
        print(f"{ts:<28} {repo_name:<20} {severity:<10} {esc_type:<35} {channels}")
    return 0


# ── Main ───────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]

    # --version / -v
    if not args or args[0] in ("--version", "-v"):
        return _cmd_version()

    # --help / -h (without subcommand)
    if args[0] in ("--help", "-h"):
        return _cmd_help()

    cmd = args[0]
    rest = args[1:]

    # help <subcommand>
    if cmd in ("help",):
        sub = rest[0] if rest else None
        return _cmd_help(sub)

    # intercept --help / -h for specific commands
    if _help_requested(rest):
        return _cmd_help(cmd)

    # ── Zero-arg commands ──
    if cmd == "init":
        return _cmd_init(rest)
    if cmd in ("install", "setup"):
        return _cmd_install()
    if cmd == "update":
        return _cmd_update()

    # ── Named-project or path commands (positional) ──
    if cmd in (
        "scan",
        "clean",
        "duster",
        "run",
        "health",
        "report",
        "ci",
        "install-hook",
        "install-cron",
    ):
        if rest and rest[0] == "--repo" and len(rest) > 1:
            name = rest[1]
            passthrough = rest[2:]
        else:
            if not _requires_name(cmd, rest):
                return 1
            name = rest[0]
            passthrough = rest[1:]
        handlers = {
            "scan": _cmd_scan,
            "clean": _cmd_clean,
            "duster": _cmd_duster,
            "run": _cmd_run,
            "health": _cmd_health,
            "report": _cmd_report,
            "ci": _cmd_ci,
            "install-hook": _cmd_install_hook,
            "install-cron": _cmd_install_cron,
        }
        fn = handlers[cmd]
        if cmd == "duster":
            return fn(name)
        return fn(name, passthrough)

    # ── Commands with optional project name ──
    if cmd == "status":
        return _cmd_status(rest)
    if cmd == "doctor":
        return _cmd_doctor(rest)
    if cmd == "dashboard":
        return _cmd_dashboard(rest)
    if cmd == "languages":
        return _cmd_languages(rest)

    # ── Named-argument commands (--repo, --mode, etc.) ──
    if cmd == "onboard":
        return _cmd_onboard(rest)
    if cmd == "upgrade-config":
        return _cmd_upgrade_config(rest)
    if cmd == "preflight":
        return _cmd_preflight(rest)
    if cmd == "heal":
        return _cmd_heal(rest)
    if cmd == "patterns":
        return _cmd_patterns(rest)
    if cmd == "campaign":
        return _cmd_campaign(rest)
    if cmd == "emergent":
        return _cmd_emergent(rest)
    if cmd == "lesson":
        return _cmd_lesson(rest)
    if cmd == "notify":
        return _cmd_notify(rest)

    # ── Path command ──
    if cmd == "secrets":
        if not rest:
            print(
                "bluei: 'secrets' needs a directory path. Try 'bluei secrets /path/to/project'.",
                file=sys.stderr,
            )
            return 1
        return _cmd_secrets(rest[0])

    # Unknown command
    print(
        f"bluei: '{cmd}' is not a command. Run 'bluei help' to see what's available.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
