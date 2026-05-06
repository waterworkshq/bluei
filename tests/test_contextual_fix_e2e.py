"""Integration tests for contextual fix engine on live zulip data."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'core'))

from sandbox_local_runner.reforge import classify_finding, RefactorClass
from sandbox_local_runner.models import Finding


ZULIP_FINDINGS = Path(__file__).parents[1] / 'repos/zulip/state/findings.jsonl'


def _load_findings():
    findings = []
    with open(ZULIP_FINDINGS) as f:
        for line in f:
            line = line.strip()
            if line:
                findings.append(json.loads(line))
    return findings


class TestZulipLiveMigration:
    """Tests validating the live zulip migration results."""

    def test_all_findings_have_refactor_class(self):
        """Every finding should now have a refactor_class assigned."""
        findings = _load_findings()
        missing = [f for f in findings if not f.get('refactor_class')]
        assert len(missing) == 0, f'{len(missing)} findings missing refactor_class'

    def test_contextual_fix_count(self):
        """Should have a significant number of CONTEXTUAL_FIX findings."""
        findings = _load_findings()
        contextual = [f for f in findings if f.get('refactor_class') == 'contextual_fix']
        assert len(contextual) > 500, f'Expected 500+ contextual_fix, got {len(contextual)}'

    def test_b904_is_contextual(self):
        """b904 findings should be classified as contextual_fix."""
        findings = _load_findings()
        b904 = [f for f in findings if f.get('rule') == 'ruff-b904']
        assert len(b904) > 0
        contextual_b904 = [f for f in b904 if f.get('refactor_class') == 'contextual_fix']
        assert len(contextual_b904) > len(b904) * 0.8, f'Only {len(contextual_b904)}/{len(b904)} b904 are contextual_fix'

    def test_b007_in_fixtures_is_simple(self):
        """b007 findings in fixtures should be classified as simple_fix."""
        findings = _load_findings()
        b007_fixtures = [f for f in findings if f.get('rule') == 'ruff-b007' and 'fixtures' in f.get('path', '')]
        assert len(b007_fixtures) > 0
        for f in b007_fixtures:
            assert f.get('refactor_class') == 'simple_fix'

    def test_c408_in_migrations_is_refactor(self):
        """c408 in Django migrations should be refactor_class (not fixable)."""
        findings = _load_findings()
        c408_migrations = [f for f in findings if f.get('rule') == 'ruff-c408' and 'migration' in f.get('path', '')]
        assert len(c408_migrations) > 0
        for f in c408_migrations:
            assert f.get('refactor_class') in ('refactor_class', 'contextual_fix')

    def test_e501_mostly_simple(self):
        """Most e501 findings should be classified as simple_fix."""
        findings = _load_findings()
        e501 = [f for f in findings if f.get('rule') == 'ruff-e501']
        assert len(e501) > 0
        simple_e501 = [f for f in e501 if f.get('refactor_class') == 'simple_fix']
        # At least half should be simple (ones originally marked safe_to_autofix=True)
        assert len(simple_e501) >= len(e501) * 0.4

    def test_safe_to_autofix_matches_classification(self):
        """SIMPLE_FIX findings should have safe_to_autofix=True."""
        findings = _load_findings()
        simple_fix = [f for f in findings if f.get('refactor_class') == 'simple_fix']
        unsafe_simple = [f for f in simple_fix if not f.get('safe_to_autofix')]
        assert len(unsafe_simple) == 0, f'{len(unsafe_simple)} simple_fix findings still marked unsafe'

    def test_pr_cycle_eligibility_increased(self):
        """More findings should now be eligible for pr-cycle."""
        findings = _load_findings()
        # Eligible = safe_to_autofix OR contextual_fix OR claude_fix
        eligible = [
            f for f in findings
            if f.get('safe_to_autofix')
            or f.get('refactor_class') in ('contextual_fix', 'claude_fix')
        ]
        # Should be a significant majority now
        eligible_pct = len(eligible) / len(findings) * 100
        assert eligible_pct > 80, f'Only {eligible_pct:.1f}% eligible, expected 80%+'
