# ADR-0021: Emergent Rule detection uses regex abstraction now, AST STRUCTURAL in alpha.5

**Status:** accepted

## Context

alpha.4 Rule Hatchery hardening requires the emergent rule detection mechanism to actually detect code patterns. Deep investigation (Phase 5 review) revealed the existing mechanism is a **stub**: `propose_rules_from_findings` (emergent_rules.py:790) and `propose_rules_from_fix_patterns` (line 854) both set `DetectionPattern.search_pattern = rule` — the LINTER RULE NAME (e.g. `"ruff-b904"`), never a code pattern. `scan_shadow_rules` (line 668) does `if needle not in line` — a substring search for the rule name in source code. A shadow scan for a `"ruff-b904"` rule finds lines containing the literal string "ruff-b904" (comments, noqa directives), not lines with `raise X` missing `from err`.

The entire detect→shadow→promote pipeline carries no real signal. Hardening its promotion gate (FP measurement, bundle-gating — grilling Q5a/Q5b) is premature without real detection. This ADR covers the detection-extraction prerequisite; the FP-source and gate decisions follow from it.

A second codebase fact shapes the decision: `DetectionType.STRUCTURAL` already exists as an enum value (emergent_rules.py:38) and `DetectionPattern.ast_pattern: Optional[str]` exists as a field, but `scan_shadow_rules` explicitly **skips** STRUCTURAL rules (line 633: "Only TEXT_PATTERN rules are scanned"). There is no AST-based scanner for emergent rules today. Building one parallels the existing `ast_engine/matcher.py` infrastructure but is a non-trivial scanner build.

## Decision

**Emergent rule detection uses regex abstraction in alpha.4; migration to AST STRUCTURAL detection lands in alpha.5 alongside Taste Atlas.**

### alpha.4 — Regex abstraction

1. **New enum value:** `DetectionType.REGEX_PATTERN` (additive; TEXT_PATTERN and STRUCTURAL unchanged).

2. **New helper:** `extract_detection_regex(snippet: str, language: str) -> str` — tokenizes the snippet's identifiers via AST walk (Python) or identifier-regex fallback (other languages), replaces each unique identifier with a numbered `\w+` capture group, and returns a regex string. Example: `raise ValueError("bad")` → `raise (?P<v1>\w+)\((?P<v2>["'].*["'])\)`.

3. **`propose_rules_from_findings` extraction step:** instead of `search_pattern=rule`, call `extract_detection_regex(finding.snippet, language)` and set `detection_type=DetectionType.REGEX_PATTERN`, `search_pattern=<regex>`. `propose_rules_from_fix_patterns` gains the same extraction from the pattern's `before_snippet`.

4. **`scan_shadow_rules` regex branch:** dispatch on `detection_type`. TEXT_PATTERN → existing substring search (`needle in line`). REGEX_PATTERN → `re.search(compiled_regex, line)`. STRUCTURAL → still skipped (alpha.5). Compilation is cached per-rule per-scan to avoid recompiling per-line.

5. **FP measurement:** new `EmergentRule.negative_examples: List[str]` additive field (defaults `[]`). `measure_false_positives(rule)` runs the rule's compiled regex against each negative example; any match is a false positive. **Bundles are NOT the FP source** (they're keyed by linter rules in a different namespace — ecologically invalid for emergent rules). Rule-owned negatives are the ecologically valid source.

6. **Promotion gate:** FP-rate only (σ). The two-tier bundle gate (τ from grilling Q5b) is **dropped** — no bundle links to an emergent rule (`asset_ref: null` on all shipped bundles; emergent rules are operator-discovered in a different namespace). The bundle gate was vacuous.

### alpha.5 — AST STRUCTURAL migration

- `scan_shadow_rules` gains a STRUCTURAL branch using the `ast_pattern` field + AST walker (parallels `ast_engine/matcher.py`).
- Existing REGEX_PATTERN rules keep working (additive; no migration).
- New rules may prefer STRUCTURAL where the regex is insufficient (complex multi-line patterns).
- alpha.5's Taste Profile provides the code-pattern substrate that makes STRUCTURAL detection meaningful.

## Rationale

- **Regex closes the immediate gap** (rule-name stub → real code-pattern detection) with a mechanism that generalizes across the main variance axis (variable names). `raise ValueError("bad")` → `raise \w+\(` catches all `raise SomeError(...)` shapes — exactly what a B904 emergent rule should detect.
- **Regex reuses the text-scanning path** with a small dispatch addition. No new scanner infrastructure. `scan_shadow_rules` already iterates files and lines; swapping substring for `re.search` is a one-branch addition.
- **AST STRUCTURAL is the right long-term answer** but is a scanner build (walk every target file's AST, match against the pattern AST). That's alpha.5 scope where Taste Atlas matures the AST tooling. Shipping it in alpha.4 would compound the already-expanded Hatchery scope.
- **The regex → AST migration is additive**, not a reversal. `DetectionType` gains `REGEX_PATTERN` now and `STRUCTURAL` gets real support later. Old regex rules still scan; new AST rules are more precise. No migration needed.
- **Rule-owned negatives for FP measurement** are ecologically valid (they're negatives FOR THIS RULE), unlike bundle negatives (which are for a different linter rule). The new `negative_examples` field is populated at proposal time from the finding's non-matching context or operator-supplied.

## Considered alternatives

- **(a) Normalized snippet as TEXT_PATTERN.** `normalize_snippet(snippet)` is whitespace-only; keeps real identifiers. Too specific — won't generalize across variable names. Rejected.
- **(b) Structural-normalized text as TEXT_PATTERN.** `_python_normalize_for_sharing` produces `<name>(<msg>)` tokenized text that won't substring-match real code. Rejected.
- **(c) Regex abstraction (alpha.4), chosen.** Generalizes across identifiers; reuses text scan; testable synthetically.
- **(d) Full AST STRUCTURAL scanner now.** Most correct; biggest build; compounds Hatchery scope expansion. Rejected for alpha.4, adopted for alpha.5.

## Consequences

- **New `DetectionType.REGEX_PATTERN`** enum value (additive; old TEXT_PATTERN rules unchanged).
- **New `EmergentRule.negative_examples: List[str]`** additive field (defaults `[]`; `to_dict`/`from_dict` round-trip; old rules load with empty list).
- **`scan_shadow_rules` gains a regex branch** with compiled-regex caching.
- **`propose_rules_from_findings` / `propose_rules_from_fix_patterns` change behavior:** `search_pattern` is now a regex derived from the snippet, not the rule name. This is a **behavior change** for any emergent rule proposed after alpha.4 — but since no live runs occur before stable, the impact is synthetic-only.
- **Bundle gate dropped** (τ → σ). The FP-rate gate via rule-owned negatives is the sole ACTIVE gate. `_passes_bundle_gate` is NOT shipped.
- **`measure_false_positives(rule)`** takes only the rule (not bundles); uses `rule.negative_examples`.
- **AST STRUCTURAL deferred to alpha.5** — `DetectionType.STRUCTURAL` remains skipped in `scan_shadow_rules` until then. Documented in seed 05 + ROADMAP alpha.5.
- **Inert in alpha.4 runtime** (no live runs). Detection extraction + FP measurement proven by synthetic tests only.
