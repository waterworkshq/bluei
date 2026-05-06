#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT="$ROOT/qa-agent"
RUNNER="$ROOT/scripts/run_and_sync.sh"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

REPO="${1:-}"
ISSUE_SCHEDULE="${ISSUE_SCHEDULE:-0 */4 * * *}"
PR_SCHEDULE="${PR_SCHEDULE:-0 */6 * * *}"
REVIEW_SCHEDULE="${REVIEW_SCHEDULE:-30 * * * *}"
MERGE_SCHEDULE="${MERGE_SCHEDULE:-0 6,18 * * *}"
INCLUDE_PR="${INCLUDE_PR:-1}"
INCLUDE_REVIEW="${INCLUDE_REVIEW:-1}"
INCLUDE_MERGE="${INCLUDE_MERGE:-1}"

if [ -z "$REPO" ]; then
  echo "Usage: $0 <repo-name>" >&2
  exit 1
fi

CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
FILTERED_CRON="$(printf '%s
' "$CURRENT_CRON" | grep -v "QA_AGENT_${REPO^^}_ISSUE" | grep -v "QA_AGENT_${REPO^^}_PR" | grep -v "QA_AGENT_${REPO^^}_REVIEW" | grep -v "QA_AGENT_${REPO^^}_MERGE" || true)"

BLOCK=$(cat <<EOF
# QA Agent ${REPO} issue-cycle
${ISSUE_SCHEDULE} ${RUNNER} ${REPO} issue-cycle --no-dry-run >> ${LOG_DIR}/qa-agent-${REPO}.log 2>&1 # QA_AGENT_${REPO^^}_ISSUE
EOF
)

if [ "$INCLUDE_PR" = "1" ]; then
  BLOCK+=$'\n'
  BLOCK+=$(cat <<EOF
# QA Agent ${REPO} pr-cycle
${PR_SCHEDULE} ${RUNNER} ${REPO} pr-cycle --no-dry-run >> ${LOG_DIR}/qa-agent-${REPO}.log 2>&1 # QA_AGENT_${REPO^^}_PR
EOF
)
fi

if [ "$INCLUDE_REVIEW" = "1" ]; then
  BLOCK+=$'\n'
  BLOCK+=$(cat <<EOF
# QA Agent ${REPO} review-cycle
${REVIEW_SCHEDULE} ${RUNNER} ${REPO} review-cycle --no-dry-run >> ${LOG_DIR}/qa-agent-${REPO}.log 2>&1 # QA_AGENT_${REPO^^}_REVIEW
EOF
)
fi

if [ "$INCLUDE_MERGE" = "1" ]; then
  BLOCK+=$'\n'
  BLOCK+=$(cat <<EOF
# QA Agent ${REPO} merge-cycle
${MERGE_SCHEDULE} ${RUNNER} ${REPO} merge-cycle --no-dry-run >> ${LOG_DIR}/qa-agent-${REPO}.log 2>&1 # QA_AGENT_${REPO^^}_MERGE
EOF
)
fi

printf '%s

%s
' "$FILTERED_CRON" "$BLOCK" | crontab -

echo "Installed QA Agent cron schedule for repo: $REPO"
echo "  issue:  $ISSUE_SCHEDULE"
if [ "$INCLUDE_PR" = "1" ]; then
  echo "  pr:     $PR_SCHEDULE"
else
  echo "  pr:     disabled"
fi
if [ "$INCLUDE_REVIEW" = "1" ]; then
  echo "  review: $REVIEW_SCHEDULE"
else
  echo "  review: disabled"
fi
if [ "$INCLUDE_MERGE" = "1" ]; then
  echo "  merge:  $MERGE_SCHEDULE"
else
  echo "  merge:  disabled"
fi
