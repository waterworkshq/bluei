# ADR-0018: Seed Library rules are synthesized and linter-validated, not mined from external sources

**Status:** accepted

Seed Library rules (Golden Validation Bundles, seeded Patterns, seeded Recipes) are produced by an LLM-synthesize-then-linter-validate pipeline, not by mining linter test suites, git history, or any external code repository. The linter serves as a validation oracle (does the before trigger the rule? is the after clean?), not as a data source.

## Context

alpha.3 needed to populate the empty alpha.2 substrate with authoritative starter rules. The original plan (seed 13 v1) proposed mining open-source repos for git-history fix commits. That was rejected — the user never wanted external repo mining. The revised plan (seed 13 v2) proposed ingesting linter test fixtures (ruff `.py` + `.snap`, eslint RuleTester). That was built and run end-to-end, producing two critical findings:

1. **Linter snapshots are diagnostic-only.** Ruff's `.snap` files contain no `fixed:`/`diff:` sections — the parser's `has_autofix` detection was broken against real data. Even after fixing the parser to execute ruff for ground-truth fixability, the generated fixtures were low quality (400-line multi-case test files dumped verbatim, not focused before/after pairs).

2. **Bulk-vendoring a linter's test suite is the wrong shape.** Committing ~45K lines of ruff's tests makes bluei a mirror of ruff, not a lever on top of it. It violates the grilling C3 decision ("structural/canonical only, no literal bulk-mined code"), creates a maintenance albatross (regenerate every ruff release), and inverts the product philosophy (leverage linters, don't absorb them).

Research across four search tools (mm-search, tavily, perplexity, web-search prime, ~20 queries) confirmed: **no authoritative, multi-language, machine-readable, freely-licensed database of code fix patterns exists as a downloadable source.** The closest things — Semgrep Registry (licensing-encumbered), CWE REST API (security-only), Quantified Code anti-patterns (Python-only, prose), OWASP Cheat Sheets (prose) — are fragmented and incomplete. The industry's actual approach (GitHub Copilot Autofix, Semgrep+GPT-4, Qlty) is to synthesize fixes with AI and validate them against detectors at runtime.

## Decision

The Seed Library is populated by **synthesizing original rule candidates with an LLM, then validating them against linters as oracles.** The pipeline:

1. **Topic selection** — survey existing rule ecosystems (ruff rule groups, semgrep categories, CWE, OWASP) to build a coverage map of categories that SHOULD exist. This is inspiration for topic coverage, not content copying.
2. **Synthesis** — for each topic, an LLM generates a small, focused canonical before (violation), after (fix), and negative example (non-triggering code). The output is original bluei-owned content.
3. **Validation** — run the relevant linter on the before (must trigger the rule) and on the after (must come back clean). Only validated candidates proceed. The linter is the oracle, not the source.
4. **Packaging** — validated candidates are packaged via the existing `package.py` into Golden Bundles + seeded Patterns, with provenance `authoritative-seed`.
5. **Loading** — product fixtures load at runtime via the existing `seeded_pattern_loader` + `open_pattern_store` factory.

The existing ruff/eslint parser execution logic (`_run_ruff_check`, `_apply_fix_and_recheck`) is repurposed as the **validator** step (step 3), not the source. The mining logic (walking fixture directories, parsing snapshot files) is dropped.

## Rationale

- **Original content, no licensing entanglement.** Synthesized examples are bluei's own work product. No attribution to ruff/eslint needed for the fixture content (the linter is just the validator).
- **Focused quality.** The LLM generates one canonical 3-10 line example per topic, not a 400-line test file. The output is exactly the "small regression fixture" the GoldenBundle schema was designed for.
- **Scalable.** One LLM call + two linter runs per topic. Parallelizable across topics via subagents.
- **Aligned with industry practice.** Copilot Autofix, Semgrep+GPT-4, and Qlty all use this model. The research confirmed it's the dominant approach.
- **Aligned with the product thesis.** The Seed Library is a *bootstrap*, not the product. The real value is the internal mining system learning from each user's codebase. The Seed Library provides starter coverage; the mining system provides the moat.
- **Aligned with alpha.4.** The Recipe Foundry (alpha.4) uses the same pattern: LLM proposes → validate → operator approves. The synthesizer applies it to fixtures; the Foundry applies it to Recipes.

## Considered alternatives

- **(A) Mine linter test suites (ruff fixtures + snapshots).** Rejected: low-quality output (multi-case test files), bulk-vendoring, license/scope mismatch. Built and rejected during alpha.3 execution.
- **(B) Hand-author canonical fixtures.** Rejected as sole approach: doesn't scale, contradicts the automation goal. Viable for a small curated set, but not the primary mechanism.
- **(C) Download a static fix-pattern database.** Rejected: research confirmed no such database exists with the right scope, licensing, and format.
- **(D) Synthesize + validate (chosen).** Original content, scalable, industry-aligned, reuses existing infrastructure.

## Consequences

- The pipeline requires an LLM at build time (not runtime). The Seed Library is a build-time artifact, not dynamically generated.
- The pipeline requires linters installed at build time (ruff, eslint) for validation. This is a build-environment dependency, not a runtime dependency.
- The shipped Seed Library is intentionally small and canonical. Comprehensive coverage grows through internal mining (the product's long-term value), not through seeding.
- The ruff_parser and eslint_parser modules (built during alpha.3) are replaced: their linter-execution logic becomes the validator; their mining logic is dropped.
- No source attribution to ruff/eslint is needed in the fixture content (it's synthesized). The validator version may be noted in metadata ("validated against ruff X.Y") but this is a verification provenance, not a derivation claim.
