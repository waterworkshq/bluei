"""sandbox_local_runner.gh — All GitHub API calls and repo helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import DETECTOR_CATALOG
from .models import Finding
from .utils import run_capture


def get_origin_url(repo_path: Path) -> str:
    rc, out = run_capture(['git', 'remote', 'get-url', 'origin'], cwd=repo_path)
    if rc != 0:
        return ''
    return out.strip()


def parse_github_repo(origin_url: str) -> tuple[str, str]:
    normalized = origin_url.strip()
    if normalized.endswith('.git'):
        normalized = normalized[:-4]

    marker = 'github.com/'
    if marker in normalized:
        slug = normalized.split(marker, 1)[1]
    elif normalized.startswith('git@github.com:'):
        slug = normalized.split(':', 1)[1]
    else:
        return '', ''

    parts = [part for part in slug.strip('/').split('/') if part]
    if len(parts) != 2:
        return '', ''
    return parts[0], parts[1]


def finding_dedupe_marker(finding_id: str) -> str:
    return f'[finding_id:{finding_id}]'


def gh_json(cmd: list[str], cwd: Path) -> Optional[Any]:
    rc, out = run_capture(cmd, cwd=cwd)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def parse_issue_number_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    match = re.search(r'/issues/(\d+)$', url)
    return int(match.group(1)) if match else None


def parse_pr_number_from_url(url: Optional[str]) -> Optional[int]:
    if not url:
        return None
    match = re.search(r'/pull/(\d+)$', url)
    return int(match.group(1)) if match else None


def find_existing_github_issue(repo_slug: str, finding_id: str, cwd: Path) -> Optional[Dict[str, Any]]:
    marker = finding_dedupe_marker(finding_id)
    payload = gh_json(
        [
            'gh',
            'issue',
            'list',
            '--repo',
            repo_slug,
            '--state',
            'all',
            '--limit',
            '200',
            '--json',
            'number,title,url,state,body',
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return None
    for issue in payload:
        body = str(issue.get('body') or '')
        if marker in body:
            return issue
    return None


def find_existing_github_pr(repo_slug: str, finding_id: str, cwd: Path) -> Optional[Dict[str, Any]]:
    marker = finding_dedupe_marker(finding_id)
    payload = gh_json(
        [
            'gh',
            'pr',
            'list',
            '--repo',
            repo_slug,
            '--state',
            'all',
            '--limit',
            '200',
            '--json',
            'number,title,url,state,body,headRefName',
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return None
    for pr in payload:
        body = str(pr.get('body') or '')
        if marker in body:
            return pr
    return None


def gh_issue_comment(repo_slug: str, issue_number: int, body: str, cwd: Path) -> bool:
    rc, _ = run_capture(
        ['gh', 'issue', 'comment', str(issue_number), '--repo', repo_slug, '--body', body],
        cwd=cwd,
    )
    return rc == 0


def gh_issue_close(repo_slug: str, issue_number: int, comment: str, cwd: Path) -> bool:
    rc, _ = run_capture(
        ['gh', 'issue', 'close', str(issue_number), '--repo', repo_slug, '--comment', comment],
        cwd=cwd,
    )
    return rc == 0


def gh_pr_comment(repo_slug: str, pr_number: int, body: str, cwd: Path) -> bool:
    rc, _ = run_capture(
        ['gh', 'pr', 'comment', str(pr_number), '--repo', repo_slug, '--body', body],
        cwd=cwd,
    )
    return rc == 0


def finding_from_issue_record(issue: Dict[str, Any]) -> Optional[Finding]:
    finding_id = str(issue.get('finding_id') or '').strip()
    path = str(issue.get('path') or '').strip()
    rule = str(issue.get('rule') or '').strip()
    rule_aliases = {
        'max-lines': 'xo-max-lines',
        'complexity': 'xo-complexity',
        'no-warning-comments': 'xo-no-warning-comments',
    }
    rule = rule_aliases.get(rule, rule)
    snippet = str(issue.get('snippet') or '').strip()
    repo = str(issue.get('repo') or 'qa-sandbox-repo').strip() or 'qa-sandbox-repo'
    if not finding_id or not path or not rule:
        return None
    try:
        line = int(issue.get('line', 0))
    except Exception:
        line = 0
    try:
        confidence = float(issue.get('confidence', 0.0))
    except Exception:
        confidence = 0.0
    rule_meta = next((entry for entry in DETECTOR_CATALOG if entry.get('rule') == rule), {})
    inferred_autofix = bool(rule_meta.get('autofix', False))
    return Finding(
        finding_id=finding_id,
        repo=repo,
        path=path,
        line=line,
        rule=rule,
        snippet=snippet,
        confidence=confidence,
        quick_win=bool(issue.get('quick_win', confidence >= 0.9)),
        safe_to_autofix=bool(issue.get('safe_to_autofix', inferred_autofix)),
    )


def repo_is_sandbox(repo_slug: str) -> bool:
    if repo_slug.endswith('/qa-sandbox-repo'):
        return True
    if repo_slug in ('vikasagarwal101/zulip', 'zulip'):
        return True
    if repo_slug in ('vikasagarwal101/ky', 'ky'):
        return True
    return False


def fetch_open_prs_for_merge(repo_slug: str, cwd: Path) -> List[Dict[str, Any]]:
    payload = gh_json(
        [
            'gh',
            'pr',
            'list',
            '--repo',
            repo_slug,
            '--state',
            'open',
            '--limit',
            '200',
            '--json',
            'number,url,title,state,isDraft,createdAt,headRefName,baseRefName',
        ],
        cwd=cwd,
    )
    if not isinstance(payload, list):
        return []

    def _sort_key(pr: Dict[str, Any]) -> tuple[int, str, int]:
        created_at = str(pr.get('createdAt') or '')
        number = int(pr.get('number') or 0)
        draft_rank = 1 if bool(pr.get('isDraft')) else 0
        return (draft_rank, created_at, number)

    return sorted(payload, key=_sort_key)


def evaluate_pr_check_health(repo_slug: str, pr_number: int, cwd: Path) -> Dict[str, Any]:
    payload = gh_json(
        [
            'gh',
            'pr',
            'view',
            str(pr_number),
            '--repo',
            repo_slug,
            '--json',
            'number,url,statusCheckRollup',
        ],
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        return {
            'eligible': True,
            'has_checks': False,
            'reason': 'checks-unavailable-proceed-cautiously',
        }

    rollup = payload.get('statusCheckRollup')
    if not isinstance(rollup, list) or len(rollup) == 0:
        return {
            'eligible': True,
            'has_checks': False,
            'reason': 'no-checks-detected-proceed-cautiously',
        }

    failing_states = {'FAILURE', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'ERROR'}
    pending_states = {'PENDING', 'IN_PROGRESS', 'QUEUED', 'EXPECTED'}
    seen_pending = False
    for check in rollup:
        state = str(check.get('conclusion') or check.get('state') or check.get('status') or '').upper()
        if state in failing_states:
            return {
                'eligible': False,
                'has_checks': True,
                'reason': f'checks-failing:{state}',
            }
        if state in pending_states:
            seen_pending = True

    if seen_pending:
        return {
            'eligible': True,
            'has_checks': True,
            'reason': 'checks-pending-no-failures',
        }

    return {
        'eligible': True,
        'has_checks': True,
        'reason': 'checks-pass-or-neutral',
    }


def evaluate_pr_reviews(repo_slug: str, pr_number: int, cwd: Path) -> Dict[str, Any]:
    """Check GitHub PR review status. Returns eligible and reason."""
    payload = gh_json(
        [
            'gh', 'pr', 'view', str(pr_number),
            '--repo', repo_slug,
            '--json', 'number,reviews,latestReviews,state,baseRefName',
        ],
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        return {'eligible': False, 'has_reviews': False, 'reason': 'reviews-unavailable-block'}

    latest = payload.get('latestReviews') or []
    states = {r.get('state') for r in latest}
    # Treat COMMENTED-only reviews as absent — they carry no substantive verdict
    substantive_states = states - {'COMMENTED', 'DISMISSED'}

    if 'CHANGES_REQUESTED' in substantive_states:
        return {'eligible': False, 'has_reviews': True, 'reason': 'changes-requested'}
    if 'PENDING' in substantive_states:
        return {'eligible': False, 'has_reviews': True, 'reason': 'review-pending'}

    approved = 'APPROVED' in substantive_states
    has_reviews = len(substantive_states) > 0

    # If reviews exist, enforce standard review policy
    if has_reviews:
        if not approved:
            return {'eligible': False, 'has_reviews': True, 'reason': 'no-approval-found'}
        return {'eligible': True, 'has_reviews': True, 'reason': 'review-check-pass'}

    # No reviews at all — check if branch protection requires them
    base_branch = payload.get('baseRefName') or 'main'
    protection = gh_json(
        ['gh', 'api', f'repos/{repo_slug}/branches/{base_branch}/protection'],
        cwd=cwd,
    )
    requires_reviews = (
        isinstance(protection, dict)
        and isinstance(protection.get('required_pull_request_reviews'), dict)
    )
    if requires_reviews:
        return {'eligible': False, 'has_reviews': False, 'reason': 'no-reviews-but-protection-requires-them'}

    # No reviews, no branch protection requiring them — proceed
    return {'eligible': True, 'has_reviews': False, 'reason': 'no-reviews-no-protection-pass'}


def evaluate_pr_mergeability(repo_slug: str, pr_number: int, cwd: Path) -> Dict[str, Any]:
    payload = gh_json(
        [
            'gh',
            'pr',
            'view',
            str(pr_number),
            '--repo',
            repo_slug,
            '--json',
            'number,url,mergeStateStatus,reviewDecision',
        ],
        cwd=cwd,
    )
    if not isinstance(payload, dict):
        return {
            'eligible': True,
            'requires_pr_fix': False,
            'merge_state_status': 'UNKNOWN',
            'reason': 'merge-state-unavailable-proceed-cautiously',
        }

    merge_state = str(payload.get('mergeStateStatus') or 'UNKNOWN').upper()
    if merge_state == 'DIRTY':
        return {
            'eligible': False,
            'requires_pr_fix': True,
            'merge_state_status': merge_state,
            'reason': 'merge-conflict-dirty',
        }
    if merge_state == 'BEHIND':
        return {
            'eligible': False,
            'requires_pr_fix': True,
            'merge_state_status': merge_state,
            'reason': 'branch-behind-base',
        }
    if merge_state == 'UNKNOWN':
        return {
            'eligible': True,
            'requires_pr_fix': False,
            'merge_state_status': merge_state,
            'reason': 'merge-state-unknown-proceed-cautiously',
        }
    if merge_state == 'UNSTABLE':
        check_health = evaluate_pr_check_health(repo_slug, pr_number, cwd)
        if check_health.get('eligible'):
            return {
                'eligible': True,
                'requires_pr_fix': False,
                'merge_state_status': merge_state,
                'reason': f"merge-state-unstable-{check_health.get('reason')}",
            }
        return {
            'eligible': False,
            'requires_pr_fix': False,
            'merge_state_status': merge_state,
            'reason': f"merge-state-unstable-{check_health.get('reason')}",
        }
    if merge_state == 'BLOCKED':
        return {
            'eligible': False,
            'requires_pr_fix': False,
            'merge_state_status': merge_state,
            'reason': 'merge-state-blocked',
        }

    return {
        'eligible': True,
        'requires_pr_fix': False,
        'merge_state_status': merge_state,
        'reason': 'mergeable-state-pass',
    }


def merge_failure_requires_pr_fix(reason: str) -> bool:
    normalized = reason.strip().lower()
    markers = (
        'not mergeable',
        'cannot be cleanly created',
        'merge conflict',
        'conflict',
        'is behind the base branch',
        'head branch is out of date',
    )
    return any(marker in normalized for marker in markers)


def merge_pr(repo_slug: str, pr_number: int, dry_run: bool, cwd: Path) -> Tuple[bool, str]:
    if dry_run:
        return True, 'dry-run-merge-simulated'
    rc, out = run_capture(
        [
            'gh',
            'pr',
            'merge',
            str(pr_number),
            '--repo',
            repo_slug,
            '--merge',
            '--delete-branch',
        ],
        cwd=cwd,
    )
    if rc == 0:
        return True, 'merged'
    return False, (out.strip() or f'gh-pr-merge-failed-rc={rc}')


def create_or_update_github_issue(
    repo_slug: str,
    finding: Finding,
    dry_run: bool,
    log_file: Path,
    cwd: Path,
) -> Dict[str, Any]:
    from .models import now_iso
    from .state import _append_text

    marker = finding_dedupe_marker(finding.finding_id)
    existing = find_existing_github_issue(repo_slug, finding.finding_id, cwd=cwd)
    sync_note = (
        f"Sandbox runner sync at {now_iso()}\\n"
        f"- rule: {finding.rule}\\n"
        f"- path: {finding.path}:{finding.line}\\n"
        f"- confidence: {finding.confidence}"
    )

    if existing:
        number = int(existing['number'])
        url = str(existing.get('url') or '')
        if dry_run:
            _append_text(log_file, f'dry-run-live: would comment existing GitHub issue #{number} finding_id={finding.finding_id}')
        else:
            gh_issue_comment(repo_slug, number, sync_note, cwd=cwd)
            _append_text(log_file, f'live: commented existing GitHub issue #{number} finding_id={finding.finding_id}')
        return {'number': number, 'url': url, 'created': False}

    title = f"[QA Sandbox] {finding.rule} in {finding.path}:{finding.line}"
    body = '\n'.join(
        [
            'Automated finding opened by sandbox_local_runner live mode.',
            '',
            marker,
            f'- dedupe_key: {finding.finding_id}',
            f'- repo: {finding.repo}',
            f'- file: {finding.path}:{finding.line}',
            f'- rule: {finding.rule}',
            f'- confidence: {finding.confidence}',
            f'- snippet: `{finding.snippet}`',
        ]
    )

    if dry_run:
        _append_text(log_file, f'dry-run-live: would create GitHub issue for finding_id={finding.finding_id}')
        return {'number': None, 'url': '', 'created': True}

    rc, out = run_capture(
        ['gh', 'issue', 'create', '--repo', repo_slug, '--title', title, '--body', body],
        cwd=cwd,
    )
    if rc != 0:
        _append_text(log_file, f'error: gh issue create failed finding_id={finding.finding_id}')
        return {'number': None, 'url': '', 'created': False, 'error': 'issue-create-failed'}
    url = out.strip().splitlines()[-1] if out.strip() else ''
    number = parse_issue_number_from_url(url)
    if number is None:
        existing_after_create = find_existing_github_issue(repo_slug, finding.finding_id, cwd=cwd)
        if existing_after_create:
            number = int(existing_after_create['number'])
            url = str(existing_after_create.get('url') or url)
    _append_text(log_file, f'live: created GitHub issue url={url} finding_id={finding.finding_id}')
    return {'number': number, 'url': url, 'created': True}


def create_or_update_github_pr(
    repo_slug: str,
    finding: Finding,
    branch: str,
    issue_number: Optional[int],
    dry_run: bool,
    log_file: Path,
    cwd: Path,
) -> Dict[str, Any]:
    from .models import now_iso
    from .state import _append_text

    marker = finding_dedupe_marker(finding.finding_id)
    existing = find_existing_github_pr(repo_slug, finding.finding_id, cwd=cwd)
    if existing:
        number = int(existing['number'])
        url = str(existing.get('url') or '')
        _append_text(log_file, f'live-idempotent: reuse existing PR #{number} finding_id={finding.finding_id}')
        return {'number': number, 'url': url, 'created': False}

    body_lines = [
        'Automated sandbox autofix PR.',
        '',
        marker,
        f'- dedupe_key: {finding.finding_id}',
        f'- rule: {finding.rule}',
        f'- file: {finding.path}:{finding.line}',
    ]
    if issue_number is not None:
        body_lines.append(f'Fixes #{issue_number}')
    body = '\n'.join(body_lines)
    title = f"fix(sandbox): {finding.rule} [{finding.path}]"

    if dry_run:
        _append_text(log_file, f'dry-run-live: would open PR from branch={branch} finding_id={finding.finding_id}')
        return {'number': None, 'url': '', 'created': True}

    rc, out = run_capture(
        ['gh', 'pr', 'create', '--repo', repo_slug, '--base', 'main', '--head', branch, '--title', title, '--body', body],
        cwd=cwd,
    )
    if rc != 0:
        _append_text(log_file, f'error: gh pr create failed branch={branch} finding_id={finding.finding_id}')
        return {'number': None, 'url': '', 'created': False, 'error': 'pr-create-failed'}
    url = out.strip().splitlines()[-1] if out.strip() else ''
    number = parse_pr_number_from_url(url)
    if number is None:
        existing_after_create = find_existing_github_pr(repo_slug, finding.finding_id, cwd=cwd)
        if existing_after_create:
            number = int(existing_after_create['number'])
            url = str(existing_after_create.get('url') or url)
    _append_text(log_file, f'live: created PR url={url} finding_id={finding.finding_id}')
    return {'number': number, 'url': url, 'created': True}


def fetch_github_live_counts(repo_path: Path) -> tuple[Optional[Dict[str, int]], str]:
    origin_url = get_origin_url(repo_path)
    if 'github.com' not in origin_url:
        return None, 'non-github-origin'

    owner, name = parse_github_repo(origin_url)
    if not owner or not name:
        return None, 'github-origin-parse-failed'

    query = (
        'query($owner:String!, $name:String!) { '
        'repository(owner:$owner, name:$name) { '
        'issues(states: OPEN) { totalCount } '
        'pullRequests(states: OPEN) { totalCount } '
        '} }'
    )
    rc, out = run_capture(
        [
            'gh',
            'api',
            'graphql',
            '-f',
            f'query={query}',
            '-F',
            f'owner={owner}',
            '-F',
            f'name={name}',
        ],
        cwd=repo_path,
    )
    if rc != 0:
        return None, f'github-origin-live-state-unavailable rc={rc}'

    try:
        payload = json.loads(out)
        repo_data = payload['data']['repository']
        return (
            {
                'open_issues': int(repo_data['issues']['totalCount']),
                'open_prs': int(repo_data['pullRequests']['totalCount']),
            },
            'github-origin-live-state',
        )
    except Exception:
        return None, 'github-origin-live-state-invalid-response'
