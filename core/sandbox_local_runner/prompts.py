"""sandbox_local_runner.prompts — All LLM prompt render functions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .constants import MAX_LINES_REFACTOR_TARGET
from .models import Finding
from .utils import command_list_to_shell


def render_test_coverage_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
) -> str:
    """Generate specialized prompt for test coverage enhancement."""
    baseline_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in baseline_checks.items())
    if not baseline_lines:
        baseline_lines = '- (none)'

    target_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in target_checks.items())
    if not target_lines:
        target_lines = '- (none for this rule)'

    snippet = finding.snippet or '(snippet unavailable)'
    return '\n'.join(
        [
            '# Test Coverage Enhancement Task',
            '',
            '## Finding metadata',
            f'- finding_id: `{finding.finding_id}`',
            f'- rule: `{finding.rule}`',
            f'- file: `{finding.path}`',
            f'- line: `{finding.line}`',
            f'- This code is not covered by tests.',
            '',
            '## Objective',
            'Write tests to cover the identified code.',
            '- Focus on the specific uncovered code',
            '- Ensure tests are meaningful and test actual behavior',
            '',
            '## Test Writing Guidelines',
            '1. **Understand the code** - Read and understand what the code does',
            '2. **Identify test cases** - What inputs/outputs should be tested?',
            '3. **Write minimal tests** - Cover the specific code, not everything',
            '4. **Use existing test patterns** - Follow the project\'s test conventions',
            '',
            '## Snippet (uncovered code)',
            '```',
            snippet,
            '```',
            '',
            '## Constraints (must follow)',
            '- Use the same test framework as existing tests in the project',
            '- Place tests in the appropriate test file/location',
            '- Do not modify the source code being tested',
            '- Focus on the uncovered code only',
            f'- max_files_changed: `{max_files_changed}`',
            f'- max_loc_diff: `{max_loc_diff}`',
            '',
            '## Validation commands',
            baseline_lines,
            '',
            '### Rule-target checks',
            target_lines,
            '',
            '## Success criteria',
            '- New tests pass',
            '- Coverage for the identified code increases',
            '- No existing tests are broken',
        ]
    ) + '\n'


def render_type_safety_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
) -> str:
    """Generate specialized prompt for type safety improvements."""
    baseline_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in baseline_checks.items())
    if not baseline_lines:
        baseline_lines = '- (none)'

    target_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in target_checks.items())
    if not target_lines:
        target_lines = '- (none for this rule)'

    snippet = finding.snippet or '(snippet unavailable)'
    return '\n'.join(
        [
            '# Type Safety Improvement Task',
            '',
            '## Finding metadata',
            f'- finding_id: `{finding.finding_id}`',
            f'- rule: `{finding.rule}`',
            f'- file: `{finding.path}`',
            f'- line: `{finding.line}`',
            f'- This code has type safety issues.',
            '',
            '## Objective',
            'Add proper type annotations to improve type safety.',
            '- Infer types from usage and context',
            '- Use specific types, not `any` or `unknown`',
            '',
            '## Type Annotation Guidelines',
            '1. **Function parameters** - Add type annotations for all parameters',
            '2. **Return types** - Explicitly declare return types',
            '3. **Variables** - Add type annotations where type inference is unclear',
            '4. **Generics** - Use generics for reusable code',
            '',
            '## Snippet (code with type issues)',
            '```',
            snippet,
            '```',
            '',
            '## Constraints (must follow)',
            '- Do not change runtime behavior',
            '- Use TypeScript types, not JSDoc',
            '- Prefer specific types over `any` or `unknown`',
            '- Keep changes minimal and focused',
            f'- max_files_changed: `{max_files_changed}`',
            f'- max_loc_diff: `{max_loc_diff}`',
            '',
            '## Validation commands',
            baseline_lines,
            '',
            '### Rule-target checks',
            target_lines,
            '',
            '## Success criteria',
            '- TypeScript compiler passes with no errors',
            '- No `any` types in the modified code',
            '- All existing tests pass',
        ]
    ) + '\n'


def render_complexity_refactor_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
) -> str:
    """Generate specialized prompt for complexity refactoring."""
    baseline_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in baseline_checks.items())
    if not baseline_lines:
        baseline_lines = '- (none)'

    target_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in target_checks.items())
    if not target_lines:
        target_lines = '- (none for this rule)'

    snippet = finding.snippet or '(snippet unavailable)'
    return '\n'.join(
        [
            '# Complexity Refactor Task',
            '',
            '## Finding metadata',
            f'- finding_id: `{finding.finding_id}`',
            f'- rule: `{finding.rule}`',
            f'- file: `{finding.path}`',
            f'- line: `{finding.line}`',
            f'- This method/function has high cyclomatic complexity and needs to be simplified.',
            '',
            '## Objective',
            'Reduce cyclomatic complexity of the identified method/function.',
            '- Target: Complexity under 20',
            '- Maintain exact same behavior',
            '',
            '## Refactoring Strategies (use as appropriate)',
            '1. **Extract Method** - Break large methods into smaller, focused methods',
            '2. **Early Returns** - Use guard clauses to reduce nesting',
            '3. **Lookup Tables** - Replace switch/if-else chains with data-driven lookups',
            '4. **Strategy Pattern** - Extract conditional logic into separate strategies',
            '5. **Simplify Conditionals** - Combine related conditions, remove redundant checks',
            '',
            '## Process',
            '1. Read and understand the method\'s purpose',
            '2. Identify distinct responsibilities within the method',
            '3. Apply appropriate refactoring pattern(s)',
            '4. Ensure all tests still pass',
            '',
            '## Snippet (context)',
            '```',
            snippet,
            '```',
            '',
            '## Constraints (must follow)',
            '- Preserve exact same behavior - no functional changes',
            '- Do not change test logic or assertions',
            '- Keep changes minimal and focused',
            f'- max_files_changed: `{max_files_changed}`',
            f'- max_loc_diff: `{max_loc_diff}`',
            '',
            '## Validation commands',
            baseline_lines,
            '',
            '### Rule-target checks',
            target_lines,
            '',
            '## Success criteria',
            '- All existing tests pass',
            '- Method complexity is reduced',
            '- No functionality is lost or changed',
        ]
    ) + '\n'


def render_maxlines_refactor_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
) -> str:
    """Generate specialized prompt for max-lines refactoring (TDD approach)."""
    baseline_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in baseline_checks.items())
    if not baseline_lines:
        baseline_lines = '- (none)'

    target_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in target_checks.items())
    if not target_lines:
        target_lines = '- (none for this rule)'

    return '\n'.join(
        [
            '# Max-Lines Refactor Task (TDD Approach)',
            '',
            '## Finding metadata',
            f'- finding_id: `{finding.finding_id}`',
            f'- rule: `{finding.rule}`',
            f'- file: `{finding.path}`',
            f'- This file exceeds the maximum line count and needs to be split.',
            '',
            '## Objective',
            f'Split `{finding.path}` into smaller, focused files.',
            f'- Target: ~{MAX_LINES_REFACTOR_TARGET} lines per file',
            f'- Limit: Maximum {MAX_LINES_REFACTOR_TARGET} lines for auto-refactor',
            '',
            '## TDD Refactor Process (follow strictly)',
            '1. **Run existing tests first** to ensure they pass',
            '   - Note any failing tests (they should pass before you start)',
            '',
            '2. **Analyze the file structure**',
            '   - Identify logical groupings (by feature, class, or functionality)',
            '   - Look for natural boundaries (imports, exports, class definitions)',
            '',
            '3. **Split the file**',
            '   - Create new files for each logical grouping',
            '   - Move related code together',
            '   - Update imports in the original file and new files',
            '   - Keep the original file as an index/barrel file if appropriate',
            '',
            '4. **Run tests again**',
            '   - All tests must pass after the refactor',
            '   - If tests fail, fix the imports or revert and try a different split',
            '',
            '## Constraints (must follow)',
            '- Do NOT change any test logic or assertions',
            '- Do NOT add new tests (only refactor existing code)',
            '- Preserve all existing functionality',
            '- Update imports in dependent files if necessary',
            f'- max_files_changed: `{max_files_changed}`',
            f'- max_loc_diff: `{max_loc_diff}`',
            '',
            '## Validation commands',
            'Run these from repo root to verify the refactor:',
            baseline_lines,
            '',
            '### Rule-target checks',
            target_lines,
            '',
            '## Success criteria',
            '- All existing tests pass',
            '- Each new file is under the target line count',
            '- No functionality is lost or changed',
        ]
    ) + '\n'


def render_claude_fix_prompt(
    finding: Finding,
    baseline_checks: Dict[str, List[str]],
    target_checks: Dict[str, List[str]],
    max_files_changed: int,
    max_loc_diff: int,
    fix_history: Optional[List[Dict[str, Any]]] = None,
    finding_record: Optional[Dict[str, Any]] = None,
    mnemo_directives: Optional[str] = None,                  # NEW (Phase 2 mnemo integration)
) -> str:
    # Use specialized prompt for max-lines refactoring
    if finding.rule == 'xo-max-lines':
        return render_maxlines_refactor_prompt(
            finding=finding,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
        )

    # Use specialized prompt for complexity refactoring
    if finding.rule == 'xo-complexity':
        return render_complexity_refactor_prompt(
            finding=finding,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
        )

    # Use specialized prompt for test coverage
    if finding.rule in ('test-coverage-branch', 'test-coverage-function'):
        return render_test_coverage_prompt(
            finding=finding,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
        )

    # Use specialized prompt for type safety
    if finding.rule in ('type-missing-return', 'type-missing-param'):
        return render_type_safety_prompt(
            finding=finding,
            baseline_checks=baseline_checks,
            target_checks=target_checks,
            max_files_changed=max_files_changed,
            max_loc_diff=max_loc_diff,
        )

    baseline_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in baseline_checks.items())
    if not baseline_lines:
        baseline_lines = '- (none)'

    target_lines = '\n'.join(f'- `{name}`: `{command_list_to_shell(cmd)}`' for name, cmd in target_checks.items())
    if not target_lines:
        target_lines = '- (none for this rule)'

    # Build all sections in the order specified by the design doc (Section 6).
    # Section ordering:
    #   1. Finding metadata
    #   2. Snippet
    #   3. Constraints
    #   4. Validation command context (Baseline + Target checks)
    #   5. Prior context from memory        ← mnemo directives (Phase 2)
    #   6. Prior context                    ← local LESSONS_LOG (Phase 1)
    #   7. Fix history                      ← finding_record from JSONL (Phase 4)

    sections: List[str] = []

    # Section 1: Finding metadata
    sections.extend([
        '# QA Autofix Task',
        '',
        '## Finding metadata',
        f'- finding_id: `{finding.finding_id}`',
        f'- rule: `{finding.rule}`',
        f'- file: `{finding.path}`',
        f'- line: `{finding.line}`',
        f'- confidence: `{finding.confidence}`',
        '',
    ])

    # Section 2: Snippet
    snippet = finding.snippet or '(snippet unavailable)'
    sections.extend([
        '## Snippet',
        '```',
        snippet,
        '```',
        '',
    ])

    # Section 3: Constraints
    sections.extend([
        '## Constraints (must follow)',
        '- Make the minimal change required to fix this finding.',
        '- Respect scope caps (do not exceed these limits):',
        f'  - max_files_changed: `{max_files_changed}`',
        f'  - max_loc_diff: `{max_loc_diff}`',
        '- No unrelated edits or refactors.',
        '- Preserve existing behavior outside this finding.',
        '',
    ])

    # Section 4: Validation command context
    sections.extend([
        '## Validation command context',
        'Run relevant checks from repo root and fail (non-zero exit) if they do not pass.',
        '',
        '### Baseline checks (always relevant)',
        baseline_lines,
        '',
        '### Rule-target checks',
        target_lines,
        '',
    ])

    # Section 5: Prior context from memory (from mnemo, Phase 2)
    # Injected AFTER Target checks and BEFORE local Prior context, per design doc Section 6.
    if mnemo_directives:
        sections.extend([
            '## Prior context from memory',
            '',
            mnemo_directives,
            '',
        ])

    # Section 6: Prior context from LESSONS_LOG.md (Phase 1 fix_history)
    if fix_history:
        sections.append('## Prior context')
        for entry in fix_history[:3]:  # Show up to 3 most recent
            date = entry.get('date', 'unknown')
            cycle = entry.get('cycle_type', 'unknown')
            status = entry.get('changed') or entry.get('broke') or '(no detail)'
            sections.append(f'- {date} ({cycle}): {status}')
        sections.append('')

    # Section 7: Fix history from findings.jsonl (Phase 2 finding_record)
    if finding_record is not None and finding_record.get('fix_attempts', 0) > 0:
        attempts = finding_record['fix_attempts']
        last_error = finding_record.get('last_fix_error') or '(none)'
        sections.extend([
            '## Fix history',
            f'- Attempts: {attempts}',
            f'- Last error: {last_error}',
            '- This is a known-difficult finding. Consider a more conservative approach.',
            '',
        ])

    return '\n'.join(sections) + '\n'
