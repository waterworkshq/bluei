#!/usr/bin/env python3
"""Health score calculation engine - Enhanced version."""

from dataclasses import dataclass, field, InitVar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json
import math
from pathlib import Path

from .models import Finding, HealthScore, Baseline, generate_id, now_iso


@dataclass
class HealthWeights:
    """Weights for health score components.

    Backward compatibility:
    - legacy `code_quality` can be passed at init time and is distributed across
      bug_quality/lint_quality/technical_debt/maintainability using the default ratio.
    """
    legacy_code_quality: InitVar[Optional[float]] = None

    # Granular components matching the 28 rules
    bug_quality: float = 0.20        # 6 rules - bugs are critical
    lint_quality: float = 0.05       # 3 rules - style issues
    technical_debt: float = 0.05     # 1 rule - todos/debt
    documentation: float = 0.10      # 5 rules - docs
    performance: float = 0.10        # 2 rules - perf issues
    test_gaps: float = 0.10          # 2 rules - missing tests
    test_coverage: float = 0.15      # 3 rules - coverage
    type_safety: float = 0.15        # 4 rules - type issues
    maintainability: float = 0.10    # 2 rules - refactor needs

    def __init__(self, code_quality: Optional[float] = None, **kwargs):
        for field_name, default in {
            'bug_quality': 0.20,
            'lint_quality': 0.05,
            'technical_debt': 0.05,
            'documentation': 0.10,
            'performance': 0.10,
            'test_gaps': 0.10,
            'test_coverage': 0.15,
            'type_safety': 0.15,
            'maintainability': 0.10,
        }.items():
            setattr(self, field_name, kwargs.pop(field_name, default))
        if kwargs:
            unknown = ', '.join(sorted(kwargs.keys()))
            raise TypeError(f'Unknown HealthWeights fields: {unknown}')
        if code_quality is not None:
            ratio_total = 0.20 + 0.05 + 0.05 + 0.10
            self.bug_quality = code_quality * (0.20 / ratio_total)
            self.lint_quality = code_quality * (0.05 / ratio_total)
            self.technical_debt = code_quality * (0.05 / ratio_total)
            self.maintainability = code_quality * (0.10 / ratio_total)

    @property
    def code_quality(self) -> float:
        return self.bug_quality + self.lint_quality + self.technical_debt + self.maintainability


@dataclass
class PriorityIssue:
    """A prioritized issue for next run."""
    finding: Finding
    priority_score: float
    urgency: str  # 'critical', 'high', 'medium', 'low'
    reason: str


class HealthEngine:
    """Calculates and tracks repository health scores with enhanced granularity."""
    
    # Rule to component mapping (28 rules → 9 components)
    RULE_TO_COMPONENT = {
        # Bug rules (6) → bug_quality
        'discount-math-sign': 'bug_quality',
        'catalog-query-not-normalized': 'bug_quality',
        'orders-tax-truncation': 'bug_quality',
        'notifications-email-no-trim': 'bug_quality',
        'notifications-type-guard-missing': 'bug_quality',
        'inventory-invalid-quantity': 'bug_quality',
        
        # Lint rules (3) → lint_quality
        'broad-except': 'lint_quality',
        'hardcoded-tmp-path': 'lint_quality',
        'trailing-whitespace': 'lint_quality',
        
        # Debt rules (1) → technical_debt
        'debt-todo-marker': 'technical_debt',
        
        # Docs rules (5) → documentation
        'docs-legacy-reference': 'documentation',
        'docs-missing-rollback': 'documentation',
        'docs-quickstart-gap': 'documentation',
        'doc-gap-uncovered-module': 'documentation',
        'doc-drift-stale-reference': 'documentation',
        
        # Performance rules (2) → performance
        'perf-pop-front-loop': 'performance',
        'perf-list-membership-loop': 'performance',
        
        # Test gap rules (2) → test_gaps
        'test-gap-missing-file': 'test_gaps',
        'test-gap-missing-case': 'test_gaps',
        
        # Test coverage rules (3) → test_coverage
        'test-coverage-branch': 'test_coverage',
        'test-coverage-function': 'test_coverage',
        'test-coverage-line': 'test_coverage',
        
        # Type safety rules (4) → type_safety
        'type-explicit-any': 'type_safety',
        'type-missing-return': 'type_safety',
        'type-missing-param': 'type_safety',
        'type-untyped-import': 'type_safety',
        
        # Refactor rules (2) → maintainability
        'xo-max-lines': 'maintainability',
        'xo-complexity': 'maintainability',
        'max-lines': 'maintainability',
        'complexity': 'maintainability',
        'no-warning-comments': 'lint_quality',
    }
    
    # Category inference for rules not in the map
    CATEGORY_INFERENCE = {
        'test': 'test_gaps',
        'coverage': 'test_coverage',
        'type': 'type_safety',
        'any': 'type_safety',
        'doc': 'documentation',
        'perf': 'performance',
        'max-lines': 'maintainability',
        'complexity': 'maintainability',
        'refactor': 'maintainability',
        'lint': 'lint_quality',
        'trailing': 'lint_quality',
        'whitespace': 'lint_quality',
        'comment': 'lint_quality',
        'warning': 'lint_quality',
        'todo': 'technical_debt',
        'fixme': 'technical_debt',
        'bug': 'bug_quality',
        'discount': 'bug_quality',
        'math': 'bug_quality',
        'query': 'bug_quality',
    }
    
    # Severity penalties by component type
    SEVERITY_PENALTY = {
        'bug_quality': {'critical': 20, 'high': 12, 'medium': 6, 'low': 2},
        'lint_quality': {'critical': 5, 'high': 3, 'medium': 1, 'low': 0.25},
        'technical_debt': {'critical': 8, 'high': 5, 'medium': 2, 'low': 0.5},
        'documentation': {'critical': 5, 'high': 3, 'medium': 1.5, 'low': 0.5},
        'performance': {'critical': 15, 'high': 8, 'medium': 3, 'low': 1},
        'test_gaps': {'critical': 12, 'high': 6, 'medium': 3, 'low': 1},
        'test_coverage': {'critical': 10, 'high': 5, 'medium': 2, 'low': 0.5},
        'type_safety': {'critical': 12, 'high': 6, 'medium': 2, 'low': 0.5},
        'maintainability': {'critical': 5, 'high': 3, 'medium': 1, 'low': 0.25},
    }
    
    # Default penalty for unknown components
    DEFAULT_PENALTY = {'critical': 10, 'high': 5, 'medium': 2, 'low': 1}
    
    # Minimum score per component (floor)
    MIN_COMPONENT_SCORE = 5.0
    
    # Baseline improvement bonus
    IMPROVEMENT_BONUS_WEIGHT = 0.1  # 10% bonus for improvement
    
    def __init__(self, weights: Optional[HealthWeights] = None):
        self.weights = weights or HealthWeights()
    
    def _get_severity(self, finding: Finding) -> str:
        """Determine severity from finding confidence."""
        if finding.confidence >= 0.90:
            return 'critical'
        elif finding.confidence >= 0.85:
            return 'high'
        elif finding.confidence >= 0.75:
            return 'medium'
        else:
            return 'low'
    
    def _get_component(self, finding: Finding) -> str:
        """Get health component for a finding."""
        # First check if rule is directly mapped
        rule_lower = finding.rule.lower()
        if rule_lower in self.RULE_TO_COMPONENT:
            return self.RULE_TO_COMPONENT[rule_lower]
        
        # Check if category is set and can be mapped
        if finding.category:
            cat_lower = finding.category.lower()
            if cat_lower in self.RULE_TO_COMPONENT:
                return self.RULE_TO_COMPONENT[cat_lower]
        
        # Infer from rule name
        for keyword, component in self.CATEGORY_INFERENCE.items():
            if keyword in rule_lower:
                return component
        
        # Default to bug_quality for unknown rules
        return 'bug_quality'
    
    def _logarithmic_penalty(self, penalty: float, count: int) -> float:
        """Apply logarithmic scaling to prevent score collapse but remain realistic."""
        if count <= 0:
            return 0
        # Use steeper log scale: penalty * (count/5) * log10(count + 1)
        # This means:
        #   5 findings = penalty × 1 × 0.78 = 0.78x
        #   10 findings = penalty × 2 × 1.04 = 2.08x
        #   50 findings = penalty × 10 × 1.71 = 17.1x
        #   100 findings = penalty × 20 × 2.0 = 40x
        #   135 findings = penalty × 27 × 2.13 = 57.5x
        return penalty * (count / 5) * math.log10(count + 1)
    
    def calc_component_score(self, 
                             component: str, 
                             findings: List[Finding]) -> float:
        """Calculate score for a single component using logarithmic scaling."""
        component_findings = [
            f for f in findings 
            if self._get_component(f) == component
        ]
        
        if not component_findings:
            return 100.0  # No issues = perfect score
        
        # Group findings by severity
        by_severity = {'critical': [], 'high': [], 'medium': [], 'low': []}
        for f in component_findings:
            severity = self._get_severity(f)
            by_severity[severity].append(f)
        
        # Get penalties for this component
        penalties = self.SEVERITY_PENALTY.get(component, self.DEFAULT_PENALTY)
        
        # Calculate total penalty using logarithmic scaling
        total_penalty = 0
        for severity, finding_list in by_severity.items():
            if finding_list:
                base_penalty = penalties.get(severity, 2)
                # Apply logarithmic scaling based on count
                scaled_penalty = self._logarithmic_penalty(base_penalty, len(finding_list))
                total_penalty += scaled_penalty
        
        # Score is 100 minus penalty, with minimum floor
        score = max(self.MIN_COMPONENT_SCORE, 100 - total_penalty)
        return min(100, score)
    
    def calculate(self, 
                  findings: List[Finding], 
                  coverage_data: Optional[Dict] = None,
                  baseline_score: Optional[float] = None) -> HealthScore:
        """Calculate overall health score with improvement bonus."""
        
        # Calculate all 9 component scores
        components = {
            'bug_quality': self.calc_component_score('bug_quality', findings),
            'lint_quality': self.calc_component_score('lint_quality', findings),
            'technical_debt': self.calc_component_score('technical_debt', findings),
            'documentation': self.calc_component_score('documentation', findings),
            'performance': self.calc_component_score('performance', findings),
            'test_gaps': self.calc_component_score('test_gaps', findings),
            'test_coverage': self.calc_component_score('test_coverage', findings),
            'type_safety': self.calc_component_score('type_safety', findings),
            'maintainability': self.calc_component_score('maintainability', findings),
        }

        # Backward-compatible aggregate alias
        code_quality_weight = (
            self.weights.bug_quality + self.weights.lint_quality +
            self.weights.technical_debt + self.weights.maintainability
        ) or 1.0
        components['code_quality'] = round((
            components['bug_quality'] * self.weights.bug_quality +
            components['lint_quality'] * self.weights.lint_quality +
            components['technical_debt'] * self.weights.technical_debt +
            components['maintainability'] * self.weights.maintainability
        ) / code_quality_weight, 1)
        
        # Override test_coverage with actual coverage if provided
        if coverage_data and 'percentage' in coverage_data:
            components['test_coverage'] = coverage_data['percentage']
        
        # Calculate weighted score
        base_score = (
            components['bug_quality'] * self.weights.bug_quality +
            components['lint_quality'] * self.weights.lint_quality +
            components['technical_debt'] * self.weights.technical_debt +
            components['documentation'] * self.weights.documentation +
            components['performance'] * self.weights.performance +
            components['test_gaps'] * self.weights.test_gaps +
            components['test_coverage'] * self.weights.test_coverage +
            components['type_safety'] * self.weights.type_safety +
            components['maintainability'] * self.weights.maintainability
        )
        
        # Add improvement bonus if baseline exists
        if baseline_score is not None:
            improvement = base_score - baseline_score
            if improvement > 0:
                # Bonus for improvement (up to 5 points max)
                bonus = min(5, improvement * self.IMPROVEMENT_BONUS_WEIGHT)
                base_score += bonus
        
        return HealthScore(
            score=round(base_score, 1),
            components=components,
            calculated_at=now_iso(),
        )
    
    def get_findings_by_component(self, 
                                   findings: List[Finding]) -> Dict[str, int]:
        """Count findings by component."""
        counts = {}
        for f in findings:
            component = self._get_component(f)
            counts[component] = counts.get(component, 0) + 1
        return counts

    def get_findings_by_category(self,
                                 findings: List[Finding]) -> Dict[str, int]:
        """Backward-compatible alias for component counts.

        Legacy callers expect broader buckets like `test_coverage` and `code_quality`.
        We preserve granular counts while also exposing those aggregate aliases.
        """
        counts = self.get_findings_by_component(findings)
        counts['code_quality'] = (
            counts.get('bug_quality', 0) + counts.get('lint_quality', 0) +
            counts.get('technical_debt', 0) + counts.get('maintainability', 0)
        )
        counts['test_coverage'] = counts.get('test_coverage', 0) + counts.get('test_gaps', 0)
        return counts
    
    def get_findings_by_severity(self, 
                                  findings: List[Finding]) -> Dict[str, int]:
        """Count findings by severity."""
        counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
        for f in findings:
            sev = self._get_severity(f)
            counts[sev] = counts.get(sev, 0) + 1
        return counts
    
    def prioritize_issues(self, 
                          findings: List[Finding],
                          max_issues: int = 10) -> List[PriorityIssue]:
        """Prioritize issues for next run based on impact and urgency."""
        prioritized = []
        
        for f in findings:
            # Calculate priority score
            component = self._get_component(f)
            severity = self._get_severity(f)
            
            # Base priority from component weight
            weight = getattr(self.weights, component, 0.1)
            
            # Severity multiplier
            severity_mult = {'critical': 4, 'high': 3, 'medium': 2, 'low': 1}[severity]
            
            # Quick win bonus (if safe to autofix)
            quick_win_bonus = 1.5 if f.safe_to_autofix else 1.0
            
            # Calculate final priority score
            priority_score = weight * severity_mult * quick_win_bonus * f.confidence
            
            # Determine urgency
            if severity == 'critical':
                urgency = 'critical'
            elif severity == 'high' or priority_score > 0.5:
                urgency = 'high'
            elif priority_score > 0.2:
                urgency = 'medium'
            else:
                urgency = 'low'
            
            # Generate reason
            reasons = []
            if f.safe_to_autofix:
                reasons.append("quick win")
            if severity in ('critical', 'high'):
                reasons.append(f"{severity} severity")
            if component == 'bug_quality':
                reasons.append("potential bug")
            reasons.append(f"affects {component.replace('_', ' ')}")
            
            prioritized.append(PriorityIssue(
                finding=f,
                priority_score=priority_score,
                urgency=urgency,
                reason=", ".join(reasons)
            ))
        
        # Sort by priority score (highest first)
        prioritized.sort(key=lambda x: x.priority_score, reverse=True)
        
        return prioritized[:max_issues]
    
    def create_baseline(self,
                        repo_id: str,
                        findings: List[Finding],
                        health: HealthScore,
                        findings_file: str) -> Baseline:
        """Create a baseline from current state."""
        return Baseline(
            id=generate_id('baseline'),
            repo_id=repo_id,
            captured_at=now_iso(),
            findings_total=len(findings),
            findings_by_category=self.get_findings_by_component(findings),
            findings_by_severity=self.get_findings_by_severity(findings),
            health_score=health.score,
            health_components=health.components,
            findings_file=findings_file,
        )
    
    def save_health_snapshot(self, 
                              repo_name: str,
                              health: HealthScore,
                              findings_count: int,
                              state_dir: Path) -> None:
        """Save a health snapshot for history."""
        history_file = state_dir / 'health_history.jsonl'
        snapshot = {
            'timestamp': now_iso(),
            'score': health.score,
            'components': health.components,
            'findings_count': findings_count,
        }
        
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, 'a') as f:
            f.write(json.dumps(snapshot) + '\n')
    
    def get_health_history(self, 
                            repo_name: str,
                            state_dir: Path,
                            days: int = 30) -> List[Dict]:
        """Get health history for a repo."""
        history_file = state_dir / 'health_history.jsonl'
        if not history_file.exists():
            return []
        
        snapshots = []
        with open(history_file) as f:
            for line in f:
                if line.strip():
                    snapshots.append(json.loads(line))
        
        # Return last N days
        return snapshots[-days:] if len(snapshots) > days else snapshots
    
    def get_improvement_summary(self,
                                 current_score: float,
                                 baseline_score: float,
                                 current_findings: Dict[str, int],
                                 baseline_findings: Dict[str, int]) -> Dict[str, Any]:
        """Generate improvement summary comparing current to baseline."""
        improvement = current_score - baseline_score
        
        # Calculate changes by component
        changes = {}
        for component in set(list(current_findings.keys()) + list(baseline_findings.keys())):
            current = current_findings.get(component, 0)
            baseline = baseline_findings.get(component, 0)
            change = baseline - current  # Positive = improvement (fewer findings)
            changes[component] = {
                'baseline': baseline,
                'current': current,
                'change': change,
                'improved': change > 0,
            }
        
        return {
            'score_improvement': improvement,
            'improved': improvement > 0,
            'component_changes': changes,
            'total_findings_reduced': sum(1 for c in changes.values() if c['improved']),
        }
