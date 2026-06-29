"""LLM prompt construction for Recipe authoring (ADR-0020).

Mirrors tools/seed/synthesizer._build_prompt: injectable callback pattern.
"""

from __future__ import annotations

from typing import List

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.pattern_store import FixPattern
from bluei.engine.recipe_schema import Recipe


def _schema_summary() -> str:
    """One-line per top-level Recipe field, mirroring recipe_schema.py."""
    return """Recipe schema (top-level fields):
  id: <unique recipe id, e.g. "foundry-ruff-b904">
  rule: <linter rule name, e.g. "ruff-b904">
  language: <"python"|"typescript"|"javascript"|"*">
  safety: <"needs_validation"|"safe"|"dangerous">
  description: <one-line summary>
  match: { type: <"rule_exact"|"regex"|"prefix">, rule?: <str>, pattern?: <regex>, prefix?: <str>, scope: <"line"|"file"> }
  replacement: { type: <"text"|"regex_substitute"|"command"|"scaffold">, value?|pattern+replacement?|command?, count: <int> }
  validation: { run_baseline: <bool>, run_target: <bool> }
  metadata: { version: <int>, author: <str>, source_pattern_id: <str>, source_bundle_id?: <str> }
  priority: <int>"""


def _gold_examples() -> str:
    """2-3 reference Recipes covering the main replacement handler types."""
    return """Reference Recipes (study their shape):

# 1) text replacement
```yaml
id: unused-import-rm
rule: "ruff-f401"
language: python
safety: safe
description: "Remove unused imports flagged by F401"
match:
  type: rule_exact
  rule: "ruff-f401"
replacement:
  type: text
  value: ""
  count: 1
validation:
  run_baseline: true
  run_target: true
metadata:
  version: 1
  author: bluei-recipe-foundry
```

# 2) regex_substitute
```yaml
id: raise-with-from
rule: "ruff-b904"
language: python
safety: needs_validation
description: "Add `from` to re-raised exceptions in except blocks"
match:
  type: rule_exact
  rule: "ruff-b904"
replacement:
  type: regex_substitute
  pattern: 'raise$'
  replacement: 'raise from None'
  count: 1
validation:
  run_baseline: true
  run_target: true
metadata:
  version: 1
  author: bluei-recipe-foundry
```

# 3) command
```yaml
id: eslint-autofix-runner
rule: "eslint-no-console"
language: javascript
safety: needs_validation
description: "Run eslint --fix on the flagged file"
match:
  type: rule_exact
  rule: "eslint-no-console"
replacement:
  type: command
  command: ["eslint", "--fix", "{file}"]
validation:
  run_baseline: true
  run_target: false
metadata:
  version: 1
  author: bluei-recipe-foundry
```"""


def _build_prompt(pattern: FixPattern, gold_examples: List[GoldenBundle]) -> str:
    """Construct the LLM prompt proposing a Recipe for a seeded Pattern."""
    bundle_hint = ""
    if gold_examples:
        first = gold_examples[0]
        bundle_hint = f"\nLinked GoldenBundle id (carry into metadata.source_bundle_id): {first.id}"

    return f"""Author a deterministic fix Recipe for the following seeded Pattern.

{_schema_summary()}

{_gold_examples()}

Pattern to author a Recipe for:
  rule: {pattern.rule}
  language: {pattern.language}
  before_snippet (the violation):
---
{pattern.before_snippet}
---
  after_snippet (the corrected code):
---
{pattern.after_snippet}
---
  source_pattern_id: {pattern.pattern_id}{bundle_hint}

Requirements:
- The Recipe's replacement MUST transform the before_snippet into the after_snippet.
- Choose the simplest replacement.type that does the job (text > regex_substitute > command).
- Carry source_pattern_id (and source_bundle_id if given) into metadata.
- Use author: bluei-recipe-foundry in metadata.

Respond with ONLY a complete Recipe YAML in a single fenced block. No prose.
"""
