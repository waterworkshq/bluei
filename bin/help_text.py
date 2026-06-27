"""Help text for all bluei CLI commands.

Extracted from bin/bluei.py to reduce its surface area."""

import sys

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
  {GREEN}repos{RESET}            {DIM}List registered projects (--all to include disabled){RESET}
  {GREEN}preflight{RESET}  <name> {DIM}Assess a project before registering{RESET}
  {GREEN}heal{RESET}       <name> {DIM}Heal dirty worktrees from transient artifacts{RESET}

  {GREEN}ci{RESET}        <name> {DIM}Generate GitHub Actions CI workflow{RESET}
  {GREEN}install-hook{RESET}  <name> {DIM}Install pre-commit hook{RESET}
  {GREEN}install-cron{RESET}  <name> {DIM}Install host cron schedule for project cycles{RESET}

  {GREEN}patterns{RESET}            {DIM}Inspect and manage learned fix patterns{RESET}
  {GREEN}campaign{RESET}            {DIM}Plan multi-finding refactor campaigns{RESET}
  {GREEN}emergent{RESET}            {DIM}Inspect proposed emergent rules{RESET}
  {GREEN}learn{RESET}               {DIM}Read-only governance inbox, status, audit, bundle{RESET}
  {GREEN}create-plugin{RESET}       {DIM}Scaffold a new discovery plugin{RESET}

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
  --skip-preflight           Skip plugin tool verification before onboarding
  --fix-engine {CYAN}<engine>{RESET}      Set fix engine (auto, deterministic, claude, opencode)
  --yes                      Skip confirmation prompts for high-risk modes
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
Supports text, JSON, HTML, and PDF formats.{RESET}

{BOLD}Usage:{RESET}
  bluei report {CYAN}<project-name>{RESET}
  bluei report {CYAN}<project-name>{RESET} --format json
  bluei report {CYAN}<project-name>{RESET} --format html -o report.html
  bluei report {CYAN}<project-name>{RESET} --watch --interval 60
  bluei report {CYAN}<project-name>{RESET} --notify-webhook https://hooks.example.com/report

{BOLD}Options:{RESET}
  --format {CYAN}<fmt>{RESET}               Output format: text, json, html, pdf
  --output, -o {CYAN}<path>{RESET}          Output file path
  --days {CYAN}<n>{RESET}                   Days of history (default: 30)
  --watch                    {DIM}Continuously regenerate report{RESET}
  --interval {CYAN}<seconds>{RESET}         Watch interval (default: 30)
  --notify-webhook {CYAN}<url>{RESET}       POST report JSON to a webhook URL
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
    "repos": f"""{BOLD}bluei repos{RESET}

{DIM}List registered projects with language, enabled status, and path.{RESET}

{BOLD}Usage:{RESET}
  bluei repos              {DIM}# List enabled projects{RESET}
  bluei repos --all        {DIM}# Include disabled projects{RESET}
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
  bluei patterns exclude {CYAN}<pattern_id> <glob>{RESET} --repo {CYAN}<name>{RESET}  {DIM}Exclude a path glob from a pattern{RESET}
  bluei patterns unexclude {CYAN}<pattern_id> <glob>{RESET} --repo {CYAN}<name>{RESET}  {DIM}Remove a path glob exclusion{RESET}

{BOLD}Subcommands:{RESET}
  list                  {DIM}List all active patterns with confidence and stats{RESET}
  show {CYAN}<id>{RESET}            {DIM}Show full details including diff patch{RESET}
  deactivate {CYAN}<id>{RESET}      {DIM}Manually deactivate a pattern (set confidence to 0){RESET}
  reactivate {CYAN}<id>{RESET}      {DIM}Reactivate a deactivated pattern (set confidence to 0.5){RESET}
  exclude {CYAN}<id> <glob>{RESET}  {DIM}Exclude a path glob from a pattern's matches{RESET}
  unexclude {CYAN}<id> <glob>{RESET}  {DIM}Remove a previously added path glob exclusion{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}            Project name (required)

{BOLD}Examples:{RESET}
  bluei patterns list --repo my-app
  bluei patterns show fp-a1b2c3d4e5f6 --repo my-app
  bluei patterns deactivate fp-a1b2c3d4e5f6 --repo my-app
  bluei patterns exclude fp-a1b2c3d4e5f6 'vendor/*' --repo my-app
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
    "create-plugin": f"""{BOLD}bluei create-plugin{RESET} {CYAN}--language <lang>{RESET} {CYAN}--tool <tool>{RESET} [{YELLOW}options{RESET}]

{DIM}Scaffold a new discovery plugin: generates the manifest, the
plugin.py skeleton, a health-mapping stub, and a matching pytest
skeleton. The new plugin lives under plugins/<id>/ and is picked up
by `bluei languages` on the next run.{RESET}

{BOLD}Usage:{RESET}
  bluei create-plugin {CYAN}--language {RESET}<lang> {CYAN}--tool {RESET}<tool> [{YELLOW}--plugin-id {CYAN}<id>{RESET}]
  bluei create-plugin {CYAN}--language python{RESET} {CYAN}--tool ruff{RESET}
  bluei create-plugin {CYAN}--language go{RESET} {CYAN}--tool staticcheck{RESET} {YELLOW}--plugin-id {CYAN}go-static{RESET}

{BOLD}Options:{RESET}
  {YELLOW}--language {CYAN}<lang>{RESET}        Target language (required)
  {YELLOW}--tool {CYAN}<tool>{RESET}            Tool binary the plugin will wrap (required)
  {YELLOW}--plugin-id {CYAN}<id>{RESET}        Override the derived id (default: plugin-<tool>)
  {YELLOW}--description {CYAN}<text>{RESET}    Free-form description (printed at scaffold time)
  {YELLOW}--author {CYAN}<name>{RESET}         Author for plugin.yaml (default: bluei)
  {YELLOW}--plugins-dir {CYAN}<dir>{RESET}     Where to write plugins/<id>/ (default: ./plugins)
  {YELLOW}--force{RESET}                      Overwrite an existing plugin dir or test file

{BOLD}Examples:{RESET}
  bluei create-plugin {CYAN}--language python{RESET} {CYAN}--tool ruff{RESET}
  bluei create-plugin {CYAN}--language javascript{RESET} {CYAN}--tool eslint{RESET} {YELLOW}--plugin-id {CYAN}js-lint{RESET}
  bluei create-plugin {CYAN}--language go{RESET} {CYAN}--tool staticcheck{RESET} {YELLOW}--force{RESET}
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
  bluei notify config --init --global     {DIM}# Interactive setup wizard{RESET}
  bluei notify config --init --repo my-project {DIM}# Per-repo setup wizard{RESET}
  bluei notify digest --repo my-project   {DIM}# Send digest now{RESET}
  bluei notify digest --all               {DIM}# Send digests for all repos{RESET}
   bluei notify log --repo my-project      {DIM}# Recent deliveries{RESET}
""",
    "learn": f"""{BOLD}bluei learn{RESET} {CYAN}<subcommand>{RESET} [{YELLOW}options{RESET}]

{DIM}Read-only governance surface and Golden Bundle creation for the
Operator Control Plane. Per ADR-0015, the read path is unified under
`learn`; write-verbs (approve, reject, pause, reactivate) stay native.{RESET}

{BOLD}Usage:{RESET}
  bluei learn inbox [--repo {CYAN}<name>{RESET}]
  bluei learn status {CYAN}<asset_ref>{RESET} [--repo {CYAN}<name>{RESET}]
  bluei learn audit {CYAN}<asset_ref>{RESET} [--repo {CYAN}<name>{RESET}]
  bluei learn bundle {CYAN}<pattern_id>{RESET} --worktree {CYAN}<path>{RESET} --from-finding {CYAN}<id>{RESET} [--repo {CYAN}<name>{RESET}]

{BOLD}Subcommands:{RESET}
  inbox                   {DIM}List pending governance approvals{RESET}
  status {CYAN}<asset_ref>{RESET}           {DIM}Show governance + native + recent SPRT evidence{RESET}
  audit {CYAN}<asset_ref>{RESET}            {DIM}Show full decision history for an asset{RESET}
  bundle {CYAN}<pattern_id>{RESET}          {DIM}Create a Golden Validation Bundle from a known-good fix{RESET}

{BOLD}Options:{RESET}
  --repo {CYAN}<name>{RESET}             Repository name (required)
  --worktree {CYAN}<path>{RESET}         Worktree path containing the target file (required for bundle)
  --from-finding {CYAN}<id>{RESET}       Source finding id for the bundle (required for bundle)
  --state-root {CYAN}<dir>{RESET}        Override repos state root for tests/tools

{BOLD}Examples:{RESET}
  bluei learn inbox --repo my-app
  bluei learn status pattern:fp-a1b2c3d4 --repo my-app
  bluei learn audit pattern:fp-a1b2c3d4 --repo my-app
  bluei learn bundle fp-a1b2c3d4 --worktree ./worktrees/fix --from-finding f-001 --repo my-app
""",
}
