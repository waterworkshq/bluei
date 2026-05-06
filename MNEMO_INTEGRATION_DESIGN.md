# Mnemo Integration Design — Directive Seeding Reranker

## Overview

mnemo integration has TWO active directions, not just one:

1. **Read (recall)**: Before each fix attempt, call `mnemo recall` via TypeScript SDK to get
   curated context from past QA sessions → inject into LLM prompt.
2. **Write (seed)**: After each fix attempt (success or failure), actively call `mnemo capture`
   to write structured knowledge into mnemo so future cycles can recall it.

**The system is self-learning**: The QA agent doesn't just passively hope sessions are
captured — it deliberately seeds mnemo with structured findings at every outcome. Over time,
the recall corpus grows with real QA experience.

**Assumption**: mnemo v1.1.0 is installed (`bun` binary available). The repo must have
`.mnemo/config.json` (run `mnemo init` in the repo root) and `rich_context_finalize_on_session_end: true`
must be set in the config. If not available, the system falls back gracefully.

---

## Design Principles

1. **Graceful degradation** — mnemo unavailability is never a fatal error. Fall back to
   local reranker silently.
2. **Caching** — same finding_id is recalled at most once per process invocation. Avoids
   redundant subprocess calls.
3. **Local reranker coexists** — mnemo provides semantic context; local reranker provides
   per-finding structured data (fix_attempts, failure_count). Both are injected.
4. **No new deps at runtime** — pure subprocess wrapper. No Python SDK import required.
5. **Transparent** — if mnemo returns nothing or fails, the prompt is identical to the
   pre-mnemo version.

---

## 1. Dependency Detection

**Tested reality** (all verified via direct testing with bun):

| Source | Status | Notes |
|---|---|---|
| `vector` | ✅ Works, fast (~1ms) | Returns 0 results initially (no sessions indexed) |
| `sessions` | ✅ Works, fast (~1ms) | Returns 0 results initially (no sessions captured yet) |
| `extractions` | ✅ Works, fast (~1ms) | Returns 0 results initially |
| `engram` | ✅ Works, fast (~1ms) | Bootstrap indexes patterns, not searchable code snippets |
| `graph` | ❌ **HANGS** | `gc.search()` hangs when `groupId` is undefined/empty |

**Critical finding**: `GraphitiClient.search` hangs indefinitely when `groupId` is `undefined` or `""`.
Passing a valid project name (e.g., `'zulip'`) as groupId works (returns in ~2s).
This is why the CLI `mnemo recall` hangs — it uses an empty groupId by default.

**Two-step check for v1**:
```
Step 1: Is `bun` available?
        → "bun --version" (5s timeout)
        → If fails: mnemo_available = False

Step 2: Can RecallTool respond?
        → Run RecallTool with vector+sessions+extractions only (graph/engram disabled)
        → If responds in <10s: mnemo_available = True
        → If hangs or times out: mnemo_available = False
```

**Cache**: `mnemo_available` is cached at module level after first check.

---

## 2. Query Construction

**When**: Called once per finding (cached by finding_id).

**Query string format**:
```
{rule} {path}:{line} {snippet_truncated_to_100_chars} fix failure pr-cycle
```

Components:
- `rule` — e.g., `RUF100`, `B007`. High signal for semantic similarity.
- `path:line` — file context. mnemo's Engram index can use this for symbol-level matching.
- `snippet[:100]` — the actual code that failed. Primary semantic anchor.
- `fix failure pr-cycle` — adds intent signal. mnemo's `smartQuery` classification uses
  this to narrow to fix-outcome messages.

**Truncation**: snippet capped at 100 chars to prevent query explosion and keep embedding
quality high (less noise).

**Example**:
```
RUF100 src/zulip/zerver/views.py:142 x = unused_var  # one-line assign fix failure pr-cycle
```

**Why include cycle type?** Fix failures and issue resolutions have different contextual
patterns. Adding the cycle type helps mnemo's smartQuery classifier route to relevant
session messages.

---

## 3. Result Processing

**Output format**: `mnemo recall --json` returns `RecallOutput` with:

```typescript
interface RecallOutput {
  formatted: string;       // Markdown for Claude Code display ← INJECT THIS
  rankedResults?: RecallResult[];
  totalResults: number;
  queryTimeMs: number;
  // ...
}
```

**Validation before injection**:

1. **Non-empty**: `formatted` must be truthy and non-empty string
2. **Length cap**: Truncate to `MAX_MNEMO_CHARS = 2000` characters. Context budget for
   the entire prompt is finite (~8k tokens). We reserve ~500 tokens (~2000 chars) for
   mnemo context.
3. **Minimum relevance**: If `totalResults == 0` or `formatted` is whitespace-only,
   treat as empty. Do not inject.
4. **Timeout**: 30s hard timeout on the subprocess call. If exceeded, fall back to
   local reranker.

**Section header**: Injected as `## Prior context from memory` — clearly labeled as
external memory so the LLM knows it's not from the current cycle.

---

## 4. Caching Strategy

**Cache key**: `finding_id` (globally unique per finding, stable across cycles).

**Cache location**: Module-level `Dict[str, Optional[str]]` in `mnemo_client.py`.

```python
_mnemo_cache: Dict[str, Optional[str]] = {}

def get_directives(finding: Finding, repo_root: str) -> Optional[str]:
    cache_key = finding.finding_id
    if cache_key in _mnemo_cache:
        return _mnemo_cache[cache_key]   # None = already tried, got nothing

    raw = _recall(finding, repo_root)
    result = _process_result(raw)
    _mnemo_cache[cache_key] = result
    return result
```

**Why not disk cache?** The same finding_id won't be processed twice in the same process
invocation. A disk cache would add complexity for no benefit in the current architecture.
If needed later, it can be added as a `findings_id → cached_directives` map in `state.json`.

**Cache invalidation**: Cleared on process exit (natural). No cross-process persistence needed.

---

## 5. Call Flow (per finding)

```
apply_claude_fix(finding, findings_file, lessons_file, ...)
│
├─ [cached] mnemo_available check
│
├─ directives = get_directives(finding, repo_root)
│     │
│     ├─ Check _mnemo_cache[finding_id] → hit: return cached
│     ├─ Check .mnemo/config.json in repo → miss: return None
│     ├─ Build query string
│     ├─ subprocess: mnemo recall --json --limit 5 --project ...
│     ├─ Validate formatted output
│     ├─ Truncate to 2000 chars
│     └─ Cache result (including None for misses)
│
├─ fix_history = load_lessons_for_finding(finding_id, lessons_file)
│     (local structured reranker — still used)
│
├─ finding_record = load_finding_record(finding_id, findings_file)
│
└─ render_claude_fix_prompt(
      finding, ...,
      fix_history=fix_history,
      finding_record=finding_record,
      mnemo_directives=directives,   # ← NEW
  )
```

---

## 6. Prompt Injection Points

**In `render_claude_fix_prompt`**:

```
## Finding metadata        ← existing
## Snippet                ← existing
## Snippet diff (if autofix) ← existing
## Baseline checks        ← existing
## Target checks          ← existing
## Prior context from memory  ← NEW (from mnemo, if available)
## Prior context          ← existing (from local LESSONS_LOG, Phase 2)
## Fix history            ← existing (from finding_record, Phase 4)
## <directive text>       ← content from mnemo
```

**Ordering rationale**:
- `## Prior context from memory` before `## Prior context` — mnemo's semantic context
  is broader and potentially higher quality; local lessons are a backup.
- `## Fix history` stays last — it's the most specific and recent signal.

**When mnemo returns nothing**: `mnemo_directives` is `None`, and the prompt is
**identical** to the pre-mnemo version. No degradation for the LLM.

---

## 7. Graceful Degradation (all failure modes)

| Failure | Behavior |
|---|---|
| `mnemo` CLI not installed | `mnemo_available = False`, local reranker only |
| `.mnemo/config.json` absent in repo | `mnemo_available = False`, local reranker only |
| `mnemo recall` times out (>30s) | Skip, use local reranker, log warning |
| `mnemo recall` returns rc != 0 | Skip, use local reranker, log warning |
| `mnemo recall` returns valid JSON but `formatted` empty | Skip, use local reranker, cache `None` |
| `mnemo recall` returns malformed JSON | Skip, use local reranker, cache `None` |
| `totalResults == 0` | Skip, use local reranker, cache `None` |
| `mnemo_directives` is `None` | Prompt renders exactly as before |

**Logging**: All skip events are logged at `DEBUG` level (not warning/error) since
degradation is expected behavior, not an error.

---

## 8. Indexing — How Fix Outcomes Enter mnemo

**Existing mechanism**: mnemo hooks (`mnemo-process-session.ts`) run on Claude Code
`SessionEnd` events. They process `~/.claude/sessions/active/session-{id}.json` files
(written by the Claude Code runtime), embed all messages, and write to the SQLite
database in the project's `.mnemo/` directory.

**The worktree + mnemo directory structure** (verified):

```
/home/vikas/.openclaw/workspace/zulip/           ← MAIN zulip git worktree
  .git/                                          ← git directory (not a file)
  .mnemo/config.json                             ← mnemo config (DOES NOT EXIST)
  zerver/, analytics/, templates/, .../          ← source files

/home/vikas/.openclaw/workspace/                 ← linked git worktree of zulip
  .git → .../workspace/zulip/.git               ← git worktree link
  qa-agent/                                      ← inside the linked worktree
    repos/zulip/                                 ← config placeholder (empty git repo)
    repos/ky/                                    ← separate repo
    repos/zulip/worktrees/qa-sandbox-v2-*/       ← NOT git worktrees (empty dirs)
```

The sandbox directories (`qa-sandbox-v2-*`) are **not git worktrees** — they were
created as regular directories (the `git worktree add` calls fail silently because the
underlying zulip repo was previously empty or in an orphan state).

**The real working directory**: `repo_path = /home/vikas/.openclaw/workspace/zulip/`
(which is a git worktree of the zulip repo). All file operations happen in the real
zulip directory.

**mnemo initialization status**: The real zulip worktree has NO `.mnemo/config.json`.
`mnemo recall` from within zulip fails with "requires Mnemo to be initialized."

**The required change for session capture**:

1. **Initialize mnemo in the real zulip repo**: `mnemo init` in `/home/vikas/.openclaw/workspace/zulip/`. This creates `.mnemo/config.json` with storage paths relative to the zulip root.

2. **No cwd change needed for session capture**: The `claude` subprocess currently runs with `cwd=worktree_path` (which is a regular directory, not a git worktree). Changing `cwd=repo_path` would give the subprocess access to the main zulip worktree AND the newly created `.mnemo/config.json`. Sessions would be captured into `workspace/zulip/.mnemo/`.

3. **EngramStore initialization**: Run `mnemo bootstrap` or equivalent in the zulip repo to build the symbol-level index. This is a one-time setup per repo.

4. **No parent_path linking needed**: The worktree is inside the main worktree (via the linked worktree hierarchy). `getMnemoHierarchy` from the worktree would find the main worktree's `.mnemo/config.json` directly — no need for explicit parent_path linking.

**Supplementary indexing**: `append_lesson()` writes structured records to `LESSONS_LOG.md`
with `finding_id` tags. This is still valuable — it's structured, searchable by finding_id
directly, and survives even if mnemo session capture is unavailable.

---

## 9. Performance Considerations

| Concern | Mitigation |
|---|---|
| `mnemo recall` latency (slow GPU, large DB) | 30s timeout; don't block cycle startup |
| Multiple findings per cycle (N calls) | Caching by finding_id; N calls ≤ findings processed |
| Startup overhead (import + config check) | Cached after first call; ~1ms after first |
| Embedding generation time (inside mnemo) | Handled by mnemo internally; not our concern |
| Prompt bloat from mnemo context | Hard cap: 2000 chars per finding |

**Concurrency**: Not needed for v1. Sequential calls are fine since mnemo recalls are
fast (typically <1s for local sqlite-vec). If a cycle has 50 findings, worst case is
~50 sequential calls = ~50s. Acceptable for now.

---

## 10. Configuration

| Setting | Default | How to change |
|---|---|---|
| mnemo availability | Auto-detect | N/A |
| Recall limit | 5 results | `MNEMO_RECALL_LIMIT` env var |
| Max chars from mnemo | 2000 | `MNEMO_MAX_CHARS` env var |
| Recall timeout | 30s | `MNEMO_TIMEOUT_SECONDS` env var |
| Disable entirely | (auto) | `MNEMO_ENABLED=0` env var |

**`MNEMO_ENABLED=0`**: Forces `mnemo_available = False`. Useful for testing, CI, or
repos where mnemo is installed but should be skipped.

---

## 11. Testability

**Unit tests**: `MnemoClient` is a plain class with no mnemo-specific imports.
Can be instantiated with `available=False` for tests that don't want to mock subprocess.

**Mock**: `MnemoClient.recall()` can be monkeypatched to return a known `RecallOutput`.

**E2E test**: `test_mnemo_integration_e2e.py` runs the full flow against a real mnemo
instance (requires mnemo installed + a repo with `.mnemo/config.json`).

**Fallback test**: `test_directive_seeding_e2e.py` already tests the fallback path
(local reranker only). This remains valid — it's the fallback when mnemo is unavailable.

---

## 12. Implementation Plan

### Phase 0 (Critical): Fix cwd for session capture
- In `lifecycle.py` `apply_claude_fix`, change `cwd=str(worktree_path)` → `cwd=str(repo_path)`
- Add `repo_path: Path` as a parameter to `apply_claude_fix`
- Update the call site in `cli.py` to pass `repo_path=repo_path`
- This ensures the `claude` subprocess creates session files in the repo root where mnemo hooks are active

### Phase 0b: Active Seeding Architecture (new section)

The QA agent actively seeds mnemo after every fix attempt. This is the self-learning loop.

**What gets seeded** (after each `apply_claude_fix` outcome):

```
session-start:
  mnemo capture --event session-start --session-id qa-{finding_id} --cwd {repo_path}

message (finding context):
  mnemo capture --event message --session-id qa-{finding_id} --role user --content "
Finding: {rule} in {path}:{line}
Snippet: {snippet[:200]}
Confidence: {confidence}
Quick fix: {quick_win}
Safe to autofix: {safe_to_autofix}
" --cwd {repo_path}

message (outcome):
  mnemo capture --event message --session-id qa-{finding_id} --role assistant --content "
Fix attempt {attempt_num} for {rule} in {path}:{line}
Outcome: {SUCCESS | FAILURE}
Error: {last_fix_error if failure}
Changes: {what_changed if success}
Lessons: {lesson_text}
" --cwd {repo_path}

session-end:
  mnemo capture --event session-end --session-id qa-{finding_id} --cwd {repo_path}
```

**Why separate sessions per finding**: Each finding gets its own session so recall can be
targeted by `finding_id` (via query). Grouping by finding also makes the rich context
extraction per-finding, giving cleaner recall results.

**Content design**: The user message sets context (what the finding is). The assistant
message records the outcome (what happened, what was learned). This mirrors how mnemo's
LLM extraction works best — structured user intent + structured assistant response.

**Fallback if mnemo unavailable**: `append_lesson()` writes to `LESSONS_LOG.md` as before.
The local reranker still works. mnemo seeding failure is non-fatal.

### Phase 1: `mnemo_client.py` (new file)
- `is_mnemo_available(repo_path) -> bool` — check `bun` binary + minimal RecallTool test (10s timeout)
  - Creates inline TypeScript script, runs via `bun run -`
  - Creates RecallTool with `projectRoot: repo_path`
  - Enables: `vector`, `sessions`, `extractions` (fast, working — tested)
  - Disables: `graph` (hangs without valid groupId), `engram` (returns 0 without searchable code)
  - 10s timeout on subprocess; if responds: mnemo_available = True
- `MnemoClient` class with:
  - `recall(finding, repo_path) -> Optional[str]` — read path (inject into prompt)
  - `seed(finding, outcome, repo_path) -> bool` — write path (self-learning). Writes 3 capture events:
    1. `session-start` — unique session per finding (`qa-{finding_id}`)
    2. `message` — structured finding context + outcome
    3. `session-end` — triggers rich context extraction
  - Returns `True` if all 3 events succeeded, `False` if any failed (non-fatal)
  - Also seeds a `file-edit` event if files were modified (for file affinity tracking)
- Internal `_build_query(finding) -> str` — format: `{rule} {path}:{line} {snippet[:100]} pr-cycle`
- Internal `_call_recall_via_bun(query, repo_path, limit) -> Optional[dict]`
  - Inline TypeScript: creates RecallTool, calls recall with safe sources, outputs JSON to stdout
  - Disables: `graph` (hangs without groupId), `engram` (no searchable code content yet)
  - Enables: `vector`, `sessions`, `extractions` (fast, working)
  - Hard timeout: 30s on subprocess
- Internal `_process_result(raw_json) -> Optional[str]`
  - Validates `formatted` field exists and is non-empty/whitespace
  - Truncates to `MNEMO_MAX_CHARS = 2000`
- Module-level `_mnemo_available_cache` and `_directives_cache` (per finding_id)
- Env var controls: `MNEMO_ENABLED`, `MNEMO_RECALL_LIMIT`, `MNEMO_MAX_CHARS`, `MNEMO_TIMEOUT_SECONDS`
- All skip events logged at DEBUG level (degradation is expected, not an error)

### Phase 2: `render_claude_fix_prompt` update (`prompts.py`)
- Add `mnemo_directives: Optional[str] = None` param
- Inject `## Prior context from memory` section when non-None
- Cap injected content at `MNEMO_MAX_CHARS` (if called directly)

### Phase 3: `apply_claude_fix` update (`lifecycle.py`)
- Instantiate `MnemoClient` (once at module level)
- Call `client.recall(finding, repo_path)` → get `mnemo_directives`
- Pass `mnemo_directives` to `render_claude_fix_prompt`
- **After subprocess completes**: call `client.seed(finding, outcome, repo_path)` to actively
  seed mnemo with structured session data (session-start, messages, session-end)
  - This is the self-learning write path
  - Called regardless of success/failure
  - Failure to seed is non-fatal (local LESSONS_LOG still written)

### Phase 4: `cli.py` orchestrator update
- Add `from .mnemo_client import is_mnemo_available` check at startup
- Log mnemo availability per repo (INFO level, once per cycle)

### Phase 5: Tests
- `test_mnemo_client.py` — unit tests for all failure modes (read + write)
- `test_mnemo_integration.py` — integration tests with mocked subprocess
- Update `test_directive_seeding.py` — add `mnemo_directives=None` cases
- E2E test (optional, requires real mnemo)
- Seeding test: verify `seed()` produces correct `mnemo capture` calls with finding context

---

## 13. Files to Modify

| File | Change |
|---|---|
| `core/sandbox_local_runner/mnemo_client.py` | **NEW** — mnemo wrapper |
| `core/sandbox_local_runner/prompts.py` | Add `mnemo_directives` param |
| `core/sandbox_local_runner/lifecycle.py` | Call mnemo, pass to prompt |
| `core/sandbox_local_runner/cli.py` | Log mnemo availability |
| `core/sandbox_local_runner/test_mnemo_client.py` | **NEW** — unit tests |
| `core/sandbox_local_runner/test_directive_seeding.py` | Add None-param cases |
| `core/sandbox_local_runner/__init__.py` | Re-export `is_mnemo_available` (optional) |

---

## 14. Session Capture Path — CONFIRMED

**mnemo init** creates `.mnemo/config.json` in the repo root with storage paths
relative to the repo. Session files from the `claude` subprocess flow through the
mnemo hooks:

```
claude subprocess (cwd=repo_path = /home/vikas/.openclaw/workspace/zulip/)
  → writes session JSON to ~/.claude/sessions/active/
  → SessionEnd hook fires
  → reads .mnemo/config.json from repo_path
  → Session files processed → embedded → stored in workspace/zulip/.mnemo/
  → recall queries workspace/zulip/.mnemo/ on next cycle
```

**cwd change confirmed**: `cwd=str(repo_path)` in `apply_claude_fix` gives the subprocess
access to the main worktree AND its `.mnemo/config.json`. This is the correct working directory.

**Session indexing is live** — once the QA agent runs a fix cycle and sessions are created,
they'll be captured and indexed. Recall will then return relevant context from those sessions.

---

## 15. Open Questions — ALL RESOLVED

All decisions are now resolved based on testing:

| Question | Resolution |
|---|---|
| Connection approach | **TypeScript SDK via `bun run`** — no changes to mnemo needed |
| Database location | **Per-repo** — `~/.mnemo/projects.json` maps project roots to their `.mnemo/` dirs |
| Sources to enable | **vector + sessions + extractions** (fast, tested working) |
| Graph source | **Disabled** — hangs without valid groupId |
| Engram source | **Disabled** — fast but returns 0 results without searchable code content |
| cwd for subprocess | **`cwd=str(repo_path)`** — enables session capture |
| mnemo changes needed | **None** — all integration is Python-side |

---

## 16. Implementation Update — Python-Native Engram (2026-03-26)

### Status: WORKING ✅

The `mnemo_client.py` has been rewritten with **Python-native SQLite access** for engram queries, bypassing the TypeScript CLI entirely. This eliminates the ~10s EngramStore initialization overhead.

### Performance
- **Fast queries**: 34-41ms per finding (direct SQLite, no subprocess)
- **Context per finding**: file relevance, pattern matches, call graph, dependencies

### Architecture

```
MnemoClient (mnemo_client.py)
├── Python-native path (fast, <50ms):
│   ├── find_relevant_files()     → engram_patterns LIKE search
│   ├── search_patterns()          → engram_patterns pattern search
│   ├── get_callers()             → engram_calls callee → caller
│   ├── get_callees()             → engram_calls caller → callee
│   ├── get_dependencies()       → engram_dependencies
│   ├── get_symbols_for_file()    → engram_patterns for file
│   └── get_context_for_finding() → builds full context string
│
└── CLI fallback path (slow, ~10s+):
    ├── recall()                  → bun subprocess (slow)
    └── seed()                    → bun subprocess (capture events)
```

### Database
- Path: `{repo}/.mnemo/db/memory.db`
- Tables: `engram_patterns` (80,418 rows), `engram_calls` (109,426 rows), `engram_dependencies` (22,616 rows)

### Integration
- `MnemoClient.recall(finding)` → calls `get_context_for_finding()` first (fast)
  → falls back to CLI if native returns nothing
- `MnemoClient.seed(finding, outcome, error, changes)` → writes via `mnemo capture` CLI
- `is_mnemo_available(repo_path)` → checks `.mnemo/db/memory.db` existence
- **Zero lifecycle changes** — existing `recall()` / `seed()` calls work with new fast implementation

### Context Quality (tested with real findings)
```
ruff-b904 @ stats.py:160 → JsonableError patterns, call graph, dependencies (34ms)
ruff-s311 @ fixtures.py:38 → random patterns, function refs (41ms)
ruff-c408 @ migrations/0015_*.py → dict() patterns, imports (41ms)
```

### Tests
- `test_mnemo_client.py`: 15/15 passing (all unavailable-path tests)
- `test_directive_seeding.py`: 38/39 passing (1 pre-existing time-sensitive failure)
