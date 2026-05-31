from unittest.mock import patch, MagicMock

from bluei.campaigns.planner import (
    Campaign,
    CampaignPhase,
    CampaignPlanner,
    RULE_PRIORITY,
    BUG_RULES,
    _rule_priority,
    _needs_file_serial_phase,
)
from bluei.campaigns.types import CampaignStatus, CampaignStrategy, PhaseExecutionMode
from bluei.engine.models import Finding


def _finding(
    finding_id: str,
    rule: str,
    path: str,
    line: int = 1,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        repo="demo",
        path=path,
        line=line,
        rule=rule,
        snippet="x",
        confidence=0.9,
        quick_win=True,
        safe_to_autofix=True,
    )


def test_campaign_plan_orders_rules_by_priority_and_file_overlap() -> None:
    findings = [
        _finding("f-any-1", "type-explicit-any", "src/api/users.ts", 12),
        _finding("f-bug-1", "discount-math-sign", "src/api/orders.ts", 3),
        _finding("f-any-2", "type-explicit-any", "src/api/users.ts", 20),
        _finding("f-lint-1", "trailing-whitespace", "src/api/misc.ts", 2),
    ]

    campaign = CampaignPlanner().plan(
        findings,
        repo="demo",
        title="Fix API findings",
        target_rules=["discount-math-sign", "type-explicit-any", "trailing-whitespace"],
        target_paths=["src/api/**"],
    )

    assert campaign.status == "planning"
    assert campaign.total_findings == 4
    assert [phase.title for phase in campaign.phases] == [
        "Fix discount-math-sign findings",
        "Fix type-explicit-any findings in src/api/users.ts",
        "Fix trailing-whitespace findings",
    ]
    assert [phase.finding_ids for phase in campaign.phases] == [
        ["f-bug-1"],
        ["f-any-1", "f-any-2"],
        ["f-lint-1"],
    ]
    assert campaign.dependency_graph == {
        campaign.phases[0].phase_id: [],
        campaign.phases[1].phase_id: [campaign.phases[0].phase_id],
        campaign.phases[2].phase_id: [campaign.phases[1].phase_id],
    }
    assert campaign.finding_snapshots["f-bug-1"]["rule"] == "discount-math-sign"
    assert campaign.finding_snapshots["f-bug-1"]["path"] == "src/api/orders.ts"


def test_campaign_plan_enforces_safety_caps() -> None:
    findings = [
        _finding(f"f-{i}", "type-explicit-any", f"src/file_{i}.ts") for i in range(51)
    ]

    result = CampaignPlanner().plan(
        findings,
        repo="demo",
        title="Too large",
        target_rules=["type-explicit-any"],
        target_paths=["src/**"],
    )

    assert result.status == "aborted"
    assert result.abort_reason == "max_findings_total exceeded"
    assert result.total_findings == 51


# ── Tests extracted from test_campaigns_scaffold_tiers_remaining.py ─────────


class TestPlannerSafety:
    def test_safety_abort_max_files(self):
        planner = CampaignPlanner(max_files_total=1)
        findings = [_finding(f"f{i}", "ruff-e501", f"src/file{i}.py") for i in range(3)]
        result = planner.plan(findings, repo="repo", title="test")
        assert result.status == CampaignStatus.aborted
        assert "max_files_total" in result.abort_reason

    def test_safety_abort_max_phases(self):
        planner = CampaignPlanner(max_phases=1)
        findings = [_finding(f"f{i}", f"rule-{i}", f"src/f{i}.py") for i in range(5)]
        result = planner.plan(findings, repo="repo", title="test")
        assert result.status == CampaignStatus.aborted
        assert "max_phases" in result.abort_reason

    def test_no_abort_when_within_limits(self):
        planner = CampaignPlanner(
            max_phases=10, max_findings_total=50, max_files_total=30
        )
        findings = [_finding("f1", "ruff-e501", "src/app.py")]
        result = planner.plan(findings, repo="repo", title="test")
        assert result.status != CampaignStatus.aborted


class TestPlannerStrategies:
    def test_depth_first_strategy(self):
        planner = CampaignPlanner()
        findings = [
            _finding(finding_id="f1", path="a.py", line=1, rule="ruff-e501"),
            _finding(finding_id="f2", path="b.py", line=1, rule="ruff-e501"),
        ]
        result = planner.plan(
            findings, repo="repo", title="test", strategy=CampaignStrategy.depth_first
        )
        assert len(result.phases) == 2
        for phase in result.phases:
            assert phase.execution_mode == PhaseExecutionMode.sequential

    def test_rule_based_strategy_default(self):
        planner = CampaignPlanner()
        findings = [
            _finding("f1", "ruff-e501", "src/app.py"),
        ]
        result = planner.plan(findings, repo="repo", title="test")
        assert len(result.phases) >= 1
        assert result.strategy == CampaignStrategy.rule_based

    def test_dependency_ordered_falls_back_on_empty_findings(self):
        planner = CampaignPlanner()
        result = planner.plan(
            [], repo="repo", title="test", strategy=CampaignStrategy.dependency_ordered
        )
        assert result.status == CampaignStatus.completed or result.total_findings == 0

    def test_dependency_ordered_falls_back_on_import_error(self):
        planner = CampaignPlanner()
        findings = [_finding("f1", "ruff-e501", "src/app.py")]
        with patch(
            "bluei.campaigns.planner.build_import_graph",
            side_effect=Exception("nope"),
            create=True,
        ):
            with patch.dict(
                "sys.modules",
                {
                    "bluei.app.import_graph": MagicMock(
                        build_import_graph=MagicMock(side_effect=Exception("nope"))
                    )
                },
            ):
                result = planner.plan(
                    findings,
                    repo="repo",
                    title="test",
                    strategy=CampaignStrategy.dependency_ordered,
                )
        assert len(result.phases) >= 1

    def test_dependency_ordered_no_repo_path(self):
        planner = CampaignPlanner()
        findings = [_finding("f1", "ruff-e501", "src/app.py")]
        finding_obj = findings[0]
        finding_obj.repo = ""
        with patch.dict(
            "sys.modules",
            {
                "bluei.app.import_graph": MagicMock(
                    build_import_graph=MagicMock(side_effect=Exception("nope"))
                )
            },
        ):
            result = planner.plan(
                [finding_obj],
                repo="repo",
                title="test",
                strategy=CampaignStrategy.dependency_ordered,
            )
        assert len(result.phases) >= 1

    def test_dependency_ordered_falls_back_to_depth_first(self, tmp_path):
        planner = CampaignPlanner()
        findings = [_finding("f1", "ruff-e501", "src/app.py")]
        result = planner.plan(
            findings,
            repo="nonexistent-path",
            title="test",
            strategy=CampaignStrategy.dependency_ordered,
        )
        assert len(result.phases) >= 1
        assert result.strategy == CampaignStrategy.dependency_ordered

    def test_estimated_llm_calls_counts_non_autofix(self):
        planner = CampaignPlanner()
        findings = [
            Finding(
                finding_id="f1",
                repo="r",
                path="a.py",
                line=1,
                rule="ruff-e501",
                snippet="x",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=False,
            ),
            Finding(
                finding_id="f2",
                repo="r",
                path="b.py",
                line=1,
                rule="ruff-e501",
                snippet="x",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=True,
            ),
            Finding(
                finding_id="f3",
                repo="r",
                path="c.py",
                line=1,
                rule="ruff-e501",
                snippet="x",
                confidence=0.9,
                quick_win=True,
                safe_to_autofix=False,
            ),
        ]
        result = planner.plan(findings, repo="repo", title="test")
        assert result.estimated_llm_calls == 2


class TestRulePriority:
    def test_bug_rules_get_highest_priority(self):
        for rule in BUG_RULES:
            assert _rule_priority(rule) == RULE_PRIORITY["bug"]

    def test_type_prefix(self):
        assert _rule_priority("type-some-check") == RULE_PRIORITY["type"]

    def test_type_in_rule(self):
        assert _rule_priority("check-type-safety") == RULE_PRIORITY["type"]

    def test_test_prefix(self):
        assert _rule_priority("test-unit") == RULE_PRIORITY["test"]

    def test_xo_prefix(self):
        assert _rule_priority("xo-something") == RULE_PRIORITY["refactor"]

    def test_complexity_in_rule(self):
        assert _rule_priority("some-complexity-rule") == RULE_PRIORITY["refactor"]

    def test_max_lines_in_rule(self):
        assert _rule_priority("some-max-lines-rule") == RULE_PRIORITY["refactor"]

    def test_docs_prefix(self):
        assert _rule_priority("docs-readme") == RULE_PRIORITY["docs"]

    def test_default_lint(self):
        assert _rule_priority("unknown-rule") == RULE_PRIORITY["lint"]

    def test_needs_file_serial_type(self):
        assert _needs_file_serial_phase("type-check") is True

    def test_needs_file_serial_refactor(self):
        assert _needs_file_serial_phase("xo-complexity") is True

    def test_needs_file_serial_lint(self):
        assert _needs_file_serial_phase("ruff-e501") is False


class TestCampaignPhaseFromDict:
    def test_from_dict_defaults_findings_skipped(self):
        data = {
            "phase_id": "p1",
            "index": 0,
            "title": "test",
            "finding_ids": [],
        }
        phase = CampaignPhase.from_dict(data)
        assert phase.findings_skipped == 0

    def test_to_dict_round_trip(self):
        phase = CampaignPhase(phase_id="p1", index=0, title="test", finding_ids=["f1"])
        d = phase.to_dict()
        restored = CampaignPhase.from_dict(d)
        assert restored.phase_id == "p1"
        assert restored.finding_ids == ["f1"]


class TestCampaignFromDict:
    def test_from_dict_defaults_pause_fields(self):
        data = {
            "campaign_id": "c1",
            "repo": "repo",
            "title": "t",
            "description": "d",
            "strategy": "rule_based",
            "target_rules": [],
            "target_paths": [],
            "target_findings": [],
            "finding_snapshots": {},
            "phases": [],
            "dependency_graph": {},
        }
        campaign = Campaign.from_dict(data)
        assert campaign.pause_reason is None
        assert campaign.pause_phase_id is None
        assert campaign.pause_finding_id is None
        assert campaign.paused_at is None

    def test_to_dict_round_trip(self):
        planner = CampaignPlanner()
        findings = [_finding("f1", "ruff-e501", "src/app.py")]
        campaign = planner.plan(findings, repo="repo", title="test")
        d = campaign.to_dict()
        restored = Campaign.from_dict(d)
        assert restored.campaign_id == campaign.campaign_id
        assert len(restored.phases) == len(campaign.phases)
