# GitNexus Integration — NOT RECOMMENDED FOR QA AGENT

> **Status: Rejected** — assessed 2026-03-25 after full trial with real QA agent findings.

## Assessment Summary

GitNexus (code-graph analysis) was evaluated as a context provider for the QA agent's fix engine.
Both repos were indexed with embeddings (`--embeddings` flag) and tested against 3,894 real findings.

**Verdict: NOT useful for current QA agent use case.**

## Why It Doesn't Fit

### Finding Types Are Syntactic
The QA agent primarily handles linter findings that are simple, localized fixes:

| Finding | Count | Fix | GitNexus Value |
|---|---|---|---|
| `ruff-e501` (line too long) | 2,093 | Wrap line | **NONE** |
| `ruff-c408` (dict() → {}) | 1,412 | Replace with `{}` | **NONE** |
| `ruff-b904` (raise from err) | 301 | Add `from err` | **PARTIAL** |
| `ruff-s311` (crypto PRNG) | 45 | random → secrets | **NONE** |
| `ruff-b007` (unused loop var) | 42 | `i` → `_` | **NONE** |

None of these require understanding call graphs, blast radius, or cross-module dependencies.

### `query` Returns Generic Results
Semantic search returns 20 results regardless of query term — mostly test files and unrelated definitions. No targeted discovery capability.

### `context` Often Finds Nothing
- Migration files: no graph nodes
- Generic names (`stats`, `fixtures`): no matches
- `impact --direction upstream`: always returns 0 (reverse edges not indexed)

### `impact --direction downstream` Is Useful But...
Only for complex cross-module bugs. Those represent <1% of current findings.

## When GitNexus WOULD Be Useful

For architectural refactors or complex cross-module logic bugs where understanding dependencies matters:

```
confirm_email_change: 77 downstream items, CRITICAL risk
do_send_messages: 74 downstream items, CRITICAL risk
```

If the QA agent ever expands to handle these, GitNexus would be valuable.

## What Was Tested

- Both repos indexed with embeddings: zulip (27,703 embeddings), ky (171 embeddings)
- `gitnexus query --repo zulip "..."` — tested with 6 query terms
- `gitnexus context --repo zulip <symbol>` — tested on 4 actual finding files
- `gitnexus impact --repo zulip <symbol> --direction downstream` — tested on 4 symbols
- MCP server via Node.js stdio — works but CLI commands are simpler

## Integration Approach (If Revisited)

The architecture would be clean:

```
gitnexus_client.py    →  CLI wrapper, is_gitnexus_available()
                        context(), query(), impact() — graceful fallback if unavailable

render_claude_fix_prompt()  →  injects GitNexus context as "Code context from GitNexus"
                                and blast radius as "Impact analysis"
```

But **not worth the complexity** until finding mix shifts to architectural issues.

## Commands for Manual Use

```bash
# Re-index with embeddings (if needed)
cd /repo/path && gitnexus analyze --force --embeddings

# Context: who calls what
gitnexus context --repo zulip <symbol_name>

# Impact: what depends on this
gitnexus impact --repo zulip <symbol_name> --direction downstream

# Semantic search
gitnexus query --repo zulip "search terms"
```

## Notes

- `--repo <name>` flag is required when multiple repos are indexed
- No `gitnexus embed` or `gitnexus list-repos` standalone commands exist
- MCP server works but CLI commands are simpler for subprocess integration
