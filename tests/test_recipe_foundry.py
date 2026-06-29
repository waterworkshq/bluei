"""Synthetic tests for the Recipe Foundry (ADR-0020, Slice 2).

Covers AC-P2-1/2/3 + gate_closed + resume_pending_promotion. The LLM callback
is mocked; the detector oracle is patched at the module boundary so tests do
not shell out to a real linter.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.governance import PromotionResult
from bluei.engine.jsonl import read_jsonl
from bluei.engine.pattern_store import FixPattern
from bluei.engine.recipe_schema import load_recipe, parse_recipe
from bluei.engine.shared_pattern_library import resume_pending_promotion
from bluei.tools.foundry.models import RecipeProposal
from bluei.tools.foundry.oracle import _run_detector_oracle
from bluei.tools.foundry.pipeline import run_foundry
from bluei.tools.foundry.proposer import propose_recipe


GOOD_YAML = """\
id: foundry-ruff-b904
rule: ruff-b904
language: python
safety: needs_validation
description: "Raise with from in except blocks"
match:
  type: rule_exact
  rule: ruff-b904
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
  source_pattern_id: seed-ruff-b904
"""


def _make_pattern(
    *,
    pattern_id: str = "seed-ruff-b904",
    rule: str = "ruff-b904",
    language: str = "python",
    before: str = "try:\n    pass\nexcept Exception:\n    raise\n",
    after: str = "try:\n    pass\nexcept Exception:\n    raise from None\n",
) -> FixPattern:
    return FixPattern(
        pattern_id=pattern_id,
        rule=rule,
        language=language,
        file_path="",
        before_snippet=before,
        after_snippet=after,
        diff_patch="",
        confidence=0.6,
        source="authoritative-seed",
    )


def _fenced(yaml_text: str) -> str:
    return f"Here is the recipe:\n```yaml\n{yaml_text}\n```\nDone."


class TestParseRecipeSplit:
    def test_parse_recipe_loads_text(self):
        recipe = parse_recipe(GOOD_YAML)
        assert recipe.id == "foundry-ruff-b904"
        assert recipe.match.type == "rule_exact"
        assert recipe.replacement.type == "regex_substitute"

    def test_load_recipe_delegates_to_parse(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text(GOOD_YAML, encoding="utf-8")
        recipe = load_recipe(p)
        assert recipe.id == "foundry-ruff-b904"

    def test_parse_recipe_rejects_non_mapping(self):
        with pytest.raises(ValueError):
            parse_recipe("- a\n- b\n")

    def test_parse_recipe_rejects_missing_required(self):
        with pytest.raises(ValueError):
            parse_recipe("language: python\n")


class TestProposeRecipe:
    def test_valid_output_returns_proposal(self):
        pattern = _make_pattern()
        cb = lambda prompt: _fenced(GOOD_YAML)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            proposal = propose_recipe(pattern, None, cb)
        assert proposal is not None
        assert proposal.detector_passed is True
        assert proposal.rejection_reason is None
        assert proposal.parsed.id == "foundry-ruff-b904"
        assert proposal.source_pattern_id == "seed-ruff-b904"
        assert "foundry-ruff-b904" in proposal.recipe_yaml

    def test_malformed_yaml_returns_none(self):
        pattern = _make_pattern()
        cb = lambda prompt: "```yaml\n: : not yaml\n```"  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            proposal = propose_recipe(pattern, None, cb)
        assert proposal is None

    def test_missing_required_field_returns_none(self):
        pattern = _make_pattern()
        bad = "language: python\ndescription: no id or rule\n"
        cb = lambda prompt: _fenced(bad)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            proposal = propose_recipe(pattern, None, cb)
        assert proposal is None

    def test_unknown_handler_type_rejected_by_proposer(self):
        pattern = _make_pattern()
        bad = GOOD_YAML.replace("regex_substitute", "magic_unknown_type")
        cb = lambda prompt: _fenced(bad)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            proposal = propose_recipe(pattern, None, cb)
        # parse_recipe accepts any str; the Foundry adds a strict allowlist
        # gate post-parse so unknown handler types never reach the write path.
        assert proposal is None

    def test_detector_failure_sets_rejection_reason(self):
        pattern = _make_pattern()
        cb = lambda prompt: _fenced(GOOD_YAML)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(False, "after still has 1 diagnostics"),
        ):
            proposal = propose_recipe(pattern, None, cb)
        assert proposal is not None
        assert proposal.detector_passed is False
        assert proposal.rejection_reason is not None
        assert "diagnostics" in proposal.rejection_reason

    def test_unknown_top_level_keys_stripped(self):
        pattern = _make_pattern()
        dirty = GOOD_YAML + "unknown_top: bogus\ngarbage: 1\n"
        cb = lambda prompt: _fenced(dirty)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            proposal = propose_recipe(pattern, None, cb)
        assert proposal is not None
        assert "unknown_top" not in proposal.recipe_yaml
        assert "garbage" not in proposal.recipe_yaml

    def test_source_bundle_id_carried_when_linked(self):
        pattern = _make_pattern()
        bundle = GoldenBundle(
            id="gb-ruff-b904",
            asset_class="pattern",
            asset_ref=None,
            rule="ruff-b904",
            rule_family="ruff-b",
            language="python",
            before="x",
            after="y",
            detector_before="",
            detector_after="",
            validation_command="",
            source_finding_id=None,
            extracted_at="",
        )
        cb = lambda prompt: _fenced(GOOD_YAML)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            proposal = propose_recipe(pattern, bundle, cb)
        assert proposal is not None
        assert proposal.source_bundle_id == "gb-ruff-b904"


class TestRunFoundryGateOpen:
    def test_writes_yaml_and_auto_promote(self, tmp_path):
        pattern = _make_pattern()
        cb = lambda prompt: _fenced(GOOD_YAML)  # noqa: E731
        staged = tmp_path / "staged"
        approvals = tmp_path / "approval_records.jsonl"

        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            summary = run_foundry(
                [pattern],
                [],
                llm_callback=cb,
                staged_dir=staged,
                config={},
                approval_records_path=approvals,
            )

        assert summary.proposed == 1
        assert summary.written == 1
        assert summary.auto_promoted == 1
        assert summary.queued_pending == 0

        recipes = sorted(staged.glob("*.yaml"))
        assert len(recipes) == 1
        assert recipes[0].name == "foundry-ruff-b904.yaml"

        records = list(read_jsonl(approvals))
        assert len(records) == 1
        rec = records[0]
        assert rec["decision"] == "auto_promote"
        assert rec["asset_ref"].startswith("recipe:")
        assert rec["asset_ref"] == "recipe:foundry-ruff-b904"
        assert rec["evidence_snapshot"]["source_pattern_id"] == "seed-ruff-b904"


class TestRunFoundryGateClosed:
    def test_queues_pending_with_cached_yaml(self, tmp_path):
        pattern = _make_pattern()
        cb = lambda prompt: _fenced(GOOD_YAML)  # noqa: E731
        staged = tmp_path / "staged"
        approvals = tmp_path / "approval_records.jsonl"
        config = {
            "learning": {
                "asset_classes": {"recipe": {"write_producing": "gate_closed"}}
            }
        }

        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            summary = run_foundry(
                [pattern],
                [],
                llm_callback=cb,
                staged_dir=staged,
                config=config,
                approval_records_path=approvals,
            )

        assert summary.queued_pending == 1
        assert summary.written == 0
        assert summary.auto_promoted == 0

        assert not staged.exists() or not list(staged.glob("*.yaml"))

        records = list(read_jsonl(approvals))
        assert len(records) == 1
        rec = records[0]
        assert rec["decision"] == "pending_approval"
        snap = rec["evidence_snapshot"]
        assert snap["recipe_id"] == "foundry-ruff-b904"
        assert "id: foundry-ruff-b904" in snap["proposed_recipe_yaml"]
        assert snap["source_pattern_id"] == "seed-ruff-b904"


class TestRunFoundryRejections:
    def test_malformed_yaml_counts_as_rejected(self, tmp_path):
        pattern = _make_pattern()
        cb = lambda prompt: "totally not yaml at all ::: "  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(True, ""),
        ):
            summary = run_foundry(
                [pattern],
                [],
                llm_callback=cb,
                staged_dir=tmp_path / "staged",
            )
        assert summary.rejected == 1
        assert summary.proposed == 0
        assert "ruff-b904" in summary.rejected_topics

    def test_detector_failure_counts_as_rejected(self, tmp_path):
        pattern = _make_pattern()
        cb = lambda prompt: _fenced(GOOD_YAML)  # noqa: E731
        with patch(
            "bluei.tools.foundry.proposer._run_detector_oracle",
            return_value=(False, "after still dirty"),
        ):
            summary = run_foundry(
                [pattern],
                [],
                llm_callback=cb,
                staged_dir=tmp_path / "staged",
            )
        assert summary.rejected == 1
        assert summary.proposed == 0


class TestResumePendingPromotion:
    def test_writes_cached_yaml_and_appends_auto_promote(self, tmp_path):
        approvals = tmp_path / "approval_records.jsonl"
        output_dir = tmp_path / "recipes"

        pending_record = {
            "asset_ref": "recipe:foundry-ruff-b904",
            "decision": "pending_approval",
            "native_state_before": "seeded-pattern",
            "native_state_after": "pending-recipe",
            "reason": "gate-closed",
            "evidence_snapshot": {
                "recipe_id": "foundry-ruff-b904",
                "proposed_recipe_yaml": GOOD_YAML,
                "source_pattern_id": "seed-ruff-b904",
                "source_bundle_id": "gb-ruff-b904",
            },
            "actor": "system:recipe-foundry",
            "timestamp": "2024-01-01T00:00:00+00:00",
        }
        approvals.write_text(json.dumps(pending_record) + "\n", encoding="utf-8")

        result = resume_pending_promotion(
            "recipe:foundry-ruff-b904", approvals, output_dir
        )
        assert result == PromotionResult.PROMOTED

        recipes = sorted(output_dir.glob("*.yaml"))
        assert len(recipes) == 1
        assert recipes[0].name == "foundry-ruff-b904.yaml"
        written = recipes[0].read_text(encoding="utf-8")
        assert "id: foundry-ruff-b904" in written

        records = list(read_jsonl(approvals))
        assert len(records) == 2
        auto = records[-1]
        assert auto["decision"] == "auto_promote"
        assert auto["asset_ref"] == "recipe:foundry-ruff-b904"

    def test_not_eligible_when_no_pending_record(self, tmp_path):
        approvals = tmp_path / "approval_records.jsonl"
        approvals.write_text("", encoding="utf-8")
        result = resume_pending_promotion(
            "recipe:missing", approvals, tmp_path / "recipes"
        )
        assert result == PromotionResult.NOT_ELIGIBLE

    def test_not_eligible_when_cached_yaml_missing(self, tmp_path):
        approvals = tmp_path / "approval_records.jsonl"
        pending_record = {
            "asset_ref": "recipe:foundry-ruff-b904",
            "decision": "pending_approval",
            "evidence_snapshot": {"recipe_id": "foundry-ruff-b904"},
        }
        approvals.write_text(json.dumps(pending_record) + "\n", encoding="utf-8")
        result = resume_pending_promotion(
            "recipe:foundry-ruff-b904", approvals, tmp_path / "recipes"
        )
        assert result == PromotionResult.NOT_ELIGIBLE

    def test_uses_most_recent_pending_record(self, tmp_path):
        approvals = tmp_path / "approval_records.jsonl"
        output_dir = tmp_path / "recipes"

        old_yaml = "id: foundry-old\nrule: ruff-b904\nlanguage: python\n"
        new_yaml = "id: foundry-new\nrule: ruff-b904\nlanguage: python\n"
        records = [
            {
                "asset_ref": "recipe:r",
                "decision": "pending_approval",
                "evidence_snapshot": {
                    "recipe_id": "foundry-old",
                    "proposed_recipe_yaml": old_yaml,
                },
            },
            {
                "asset_ref": "recipe:r",
                "decision": "auto_promote",
                "evidence_snapshot": {"recipe_id": "foundry-old"},
            },
            {
                "asset_ref": "recipe:r",
                "decision": "pending_approval",
                "evidence_snapshot": {
                    "recipe_id": "foundry-new",
                    "proposed_recipe_yaml": new_yaml,
                },
            },
        ]
        approvals.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
        )

        result = resume_pending_promotion("recipe:r", approvals, output_dir)
        assert result == PromotionResult.PROMOTED
        recipes = sorted(output_dir.glob("*.yaml"))
        assert recipes[0].name == "foundry-new.yaml"


class TestOracleIntegration:
    def test_oracle_constructs_parsed_rule_and_dispatches(self):
        with patch("bluei.tools.foundry.oracle.validate_candidate") as mock_val:
            from bluei.tools.seed.validator import ValidationResult

            mock_val.return_value = ValidationResult(
                valid=True,
                before_triggered=True,
                after_clean=True,
                before_diagnostics=1,
                after_diagnostics=0,
                reason="",
            )
            passed, reason = _run_detector_oracle(
                "ruff-b904",
                "try:\n    pass\nexcept Exception:\n    raise\n",
                "try:\n    pass\nexcept Exception:\n    raise from None\n",
                "python",
            )
        assert passed is True
        assert reason == ""
        args, kwargs = mock_val.call_args
        candidate = args[0]
        assert candidate.rule == "ruff-b904"
        assert candidate.source_linter == "ruff"
        assert "raise" in candidate.before
