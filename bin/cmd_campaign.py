"""Campaign command handler.

Extracted from bin/bluei.py to reduce its surface area."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

from bin.help_text import HELP_TEXT
from bin.cmd_utils import (
    has_flag,
    parse_csv_option,
    parse_option,
    parse_positive_int,
    parse_repo_arg,
)

# Backward-compat aliases for tests that import these names from bin.bluei
_parse_repo_arg = parse_repo_arg
_parse_option = parse_option
_parse_csv_option = parse_csv_option
_has_flag = has_flag
_parse_positive_int = parse_positive_int

try:
    from bluei.campaigns.types import CampaignStatus, PhaseStatus
except ImportError:
    CampaignStatus = None
    PhaseStatus = None


def _load_campaign_check_source(config_manager, repo: str):
    """Load the registered baseline checks and limits for a mutating run.

    Registered repo config only — no inference, no samples (Stage 3 A10:
    a mutating run without these is refused as unqualified downstream).
    The registered ``limits`` propagate to the executor's scope caps;
    absent entries fall back to the explicit product defaults only.

    Returns:
        Tuple (named checks dict, source repo path or None, limits dict).
    """
    try:
        repo_config = config_manager.load_repo_config(repo)
    except Exception:
        repo_config = None
    if repo_config is None:
        return {}, None, {}
    checks = {
        f"baseline-{index}": list(command)
        for index, command in enumerate(repo_config.baseline_checks or [])
        if command
    }
    source = Path(repo_config.path) if repo_config.path else None
    return checks, source, dict(getattr(repo_config, "limits", None) or {})


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
    # The registered check source must follow --state-root too: with an
    # explicit repos root the effective workspace is its parent (standard
    # layout <workspace>/repos/<repo>/config.yaml).
    config_manager = ConfigManager(
        workspace=Path(state_root).parent if state_root else None
    )
    repos_dir = Path(state_root) if state_root else config_manager.repos_dir
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
        # Inject the app-layer emergent-rule evidence consumer so an
        # emergent-rule learning objective advances the target rule + proposes
        # new ones. Injected (not imported by campaigns) — keeps campaigns
        # free of bluei.app.* (layering invariant). Only the --allow-mutate
        # branch wires it; the dry-run branch never gathers evidence.
        _emergent_consumer = None
        try:
            from functools import partial

            from bluei.app.campaign_evidence import consume_emergent_evidence

            _emergent_store = state.get_emergent_rules_file(repo)
            _emergent_consumer = partial(
                consume_emergent_evidence, emergent_store_path=_emergent_store
            )
        except Exception:
            pass
        try:
            if allow_mutate:
                # A03/A10: registered repo config is the actual check source
                # for a mutating run (no inference, no samples). An empty or
                # unregistered check set leaves the executor to refuse the
                # run as unqualified before any file effect.
                baseline_checks, source_repo, registered_limits = (
                    _load_campaign_check_source(config_manager, repo)
                )
                result = CampaignExecutor(
                    campaign_store,
                    fix_runner=autofix_fix_runner,
                    pattern_store_path=_psp,
                    emergent_evidence_consumer=_emergent_consumer,
                    baseline_checks=baseline_checks,
                    source_repo=source_repo,
                    # F2: registered limits are the real caps — the defaults
                    # are explicit fallbacks for absent entries only.
                    max_files_changed=int(
                        registered_limits.get("max_files_changed", 5)
                    ),
                    max_loc_diff=int(registered_limits.get("max_loc_diff", 200)),
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
