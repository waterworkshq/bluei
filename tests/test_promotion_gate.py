"""Phase 4 (alpha.2) — promotion gate tests.

Covers ``promote_pattern_to_recipe`` re-routed through the governance gate
(ADR-0007):

* NOT_ELIGIBLE — eligibility fails.
* PENDING_APPROVAL — gate-closed + bundle references the pattern →
  ApprovalRecord queued, no YAML written.
* NOT_ELIGIBLE — gate-closed + no bundle reference → blocked.
* PROMOTED — gate-open → YAML written, ApprovalRecord appended.
* Backward compat — ``config={}`` → gate-open behavior.
* No audit path — ``approval_records_path=None`` → no crash, YAML written.
"""

import yaml

from bluei.engine.bundle_loader import GoldenBundle
from bluei.engine.governance import PromotionResult
from bluei.engine.jsonl import read_jsonl
from bluei.engine.shared_pattern_library import (
    CrossRepoPattern,
    promote_pattern_to_recipe,
)


def _eligible_pattern(pattern_id="xp-gate-test-abc12345"):
    return CrossRepoPattern(
        pattern_id=pattern_id,
        rule="ruff-b904",
        language="python",
        structural_hash="deadbeefcafe1234",
        before_snippet="raise ValueError(msg)",
        after_snippet="raise ValueError(msg) from cause",
        confidence=0.96,
        source_repos={"repo-a", "repo-b", "repo-c", "repo-d", "repo-e"},
        success_count=25,
        failure_count=0,
    )


def _gate_closed_config():
    return {
        "learning": {
            "asset_classes": {
                "pattern": {"write_producing": "gate_closed"},
            },
        },
    }


def _gate_open_config():
    return {
        "learning": {
            "asset_classes": {
                "pattern": {"write_producing": "gate_open_audit"},
            },
        },
    }


def _write_bundle(repo_state_dir, asset_ref):
    """Drop a repo-state golden bundle referencing *asset_ref*."""
    bundle_dir = repo_state_dir / "golden_bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle = GoldenBundle(
        id="bnd-test",
        asset_class="pattern",
        asset_ref=asset_ref,
        rule="ruff-b904",
        rule_family="raise-from",
        language="python",
        before="raise ValueError(msg)",
        after="raise ValueError(msg) from cause",
        detector_before="raise ValueError(msg)",
        detector_after="raise ValueError(msg) from cause",
        validation_command="true",
        source_finding_id=None,
        extracted_at="2025-01-01T00:00:00Z",
    )
    (bundle_dir / "bnd-test.yaml").write_text(
        yaml.safe_dump(bundle.__dict__, sort_keys=False),
        encoding="utf-8",
    )


class TestNotEligible:
    def test_eligibility_failure_returns_not_eligible(self, tmp_path):
        pat = _eligible_pattern()
        pat.source_repos = {"repo-a"}  # below PROMOTION_MIN_REPOS
        result = promote_pattern_to_recipe(
            pat,
            "python",
            tmp_path / "staged",
            config=_gate_closed_config(),
        )
        assert result == PromotionResult.NOT_ELIGIBLE
        assert list((tmp_path / "staged").glob("*.yaml")) == []
        # No records written regardless of audit path.
        records_path = tmp_path / "records.jsonl"
        promote_pattern_to_recipe(
            pat,
            "python",
            tmp_path / "staged2",
            config=_gate_closed_config(),
            approval_records_path=records_path,
        )
        assert read_jsonl(records_path) == []


class TestPendingApprovalGateClosed:
    def test_gate_closed_with_bundle_queues_approval(self, tmp_path):
        pat = _eligible_pattern()
        asset_ref = f"pattern:{pat.pattern_id}"
        _write_bundle(tmp_path / "state", asset_ref)
        records_path = tmp_path / "records.jsonl"
        staged = tmp_path / "staged"

        result = promote_pattern_to_recipe(
            pat,
            "python",
            staged,
            config=_gate_closed_config(),
            repo_state_dir=tmp_path / "state",
            approval_records_path=records_path,
        )
        assert result == PromotionResult.PENDING_APPROVAL
        # No YAML written.
        assert not staged.exists() or list(staged.glob("*.yaml")) == []

        records = read_jsonl(records_path)
        assert len(records) == 1
        assert records[0]["decision"] == "pending_approval"
        assert records[0]["asset_ref"] == asset_ref
        assert records[0]["native_state_before"] == "cross-repo-pattern"
        assert records[0]["native_state_after"] == "pending-recipe"
        assert records[0]["actor"] == "system:auto-promote"
        assert records[0]["evidence_snapshot"]["pattern_id"] == pat.pattern_id

    def test_gate_closed_no_audit_path_still_pending(self, tmp_path):
        pat = _eligible_pattern()
        _write_bundle(tmp_path / "state", f"pattern:{pat.pattern_id}")
        result = promote_pattern_to_recipe(
            pat,
            "python",
            tmp_path / "staged",
            config=_gate_closed_config(),
            repo_state_dir=tmp_path / "state",
            approval_records_path=None,
        )
        assert result == PromotionResult.PENDING_APPROVAL
        assert list((tmp_path / "staged").glob("*.yaml")) == []


class TestNotEligibleGateClosedNoBundle:
    def test_gate_closed_without_bundle_returns_not_eligible(self, tmp_path):
        pat = _eligible_pattern()
        records_path = tmp_path / "records.jsonl"
        result = promote_pattern_to_recipe(
            pat,
            "python",
            tmp_path / "staged",
            config=_gate_closed_config(),
            repo_state_dir=tmp_path / "state",  # exists but no bundle refs the pattern
            approval_records_path=records_path,
        )
        assert result == PromotionResult.NOT_ELIGIBLE
        assert list((tmp_path / "staged").glob("*.yaml")) == []
        assert read_jsonl(records_path) == []

    def test_gate_closed_no_repo_state_dir_returns_not_eligible(self, tmp_path):
        pat = _eligible_pattern()
        result = promote_pattern_to_recipe(
            pat,
            "python",
            tmp_path / "staged",
            config=_gate_closed_config(),
            repo_state_dir=None,  # no bundles loaded at all
        )
        assert result == PromotionResult.NOT_ELIGIBLE


class TestPromotedGateOpen:
    def test_gate_open_writes_yaml_and_audits(self, tmp_path):
        pat = _eligible_pattern()
        records_path = tmp_path / "records.jsonl"
        staged = tmp_path / "staged"
        result = promote_pattern_to_recipe(
            pat,
            "python",
            staged,
            config=_gate_open_config(),
            approval_records_path=records_path,
        )
        assert result == PromotionResult.PROMOTED

        yamls = sorted(staged.glob("*.yaml"))
        assert len(yamls) == 1
        loaded = yaml.safe_load(yamls[0].read_text(encoding="utf-8"))
        assert loaded["rule"] == "ruff-b904"

        records = read_jsonl(records_path)
        assert len(records) == 1
        assert records[0]["decision"] == "auto_promote"
        assert records[0]["native_state_after"] == "staged-recipe"
        assert "recipe_id" in records[0]["evidence_snapshot"]


class TestBackwardCompat:
    def test_empty_config_holds_gate_closed(self, tmp_path):
        """``config={}`` → gate-CLOSED default for write-producing promotions
        (ADR-0007: "nothing writes without bluei learn approve"). Without a
        bundle reference the gate-closed path returns NOT_ELIGIBLE — no YAML,
        no ApprovalRecord."""
        pat = _eligible_pattern()
        staged = tmp_path / "staged"
        records_path = tmp_path / "records.jsonl"
        result = promote_pattern_to_recipe(
            pat,
            "python",
            staged,
            config={},
            repo_state_dir=tmp_path / "state",  # exists, no bundle refs pattern
            approval_records_path=records_path,
        )
        assert result == PromotionResult.NOT_ELIGIBLE
        assert not staged.exists() or list(staged.glob("*.yaml")) == []
        assert read_jsonl(records_path) == []

    def test_empty_config_with_bundle_queues_approval(self, tmp_path):
        """``config={}`` + a bundle referencing the pattern → the gate-closed
        default queues a pending approval instead of writing."""
        pat = _eligible_pattern()
        asset_ref = f"pattern:{pat.pattern_id}"
        _write_bundle(tmp_path / "state", asset_ref)
        staged = tmp_path / "staged"
        records_path = tmp_path / "records.jsonl"
        result = promote_pattern_to_recipe(
            pat,
            "python",
            staged,
            config={},
            repo_state_dir=tmp_path / "state",
            approval_records_path=records_path,
        )
        assert result == PromotionResult.PENDING_APPROVAL
        assert not staged.exists() or list(staged.glob("*.yaml")) == []
        records = read_jsonl(records_path)
        assert len(records) == 1
        assert records[0]["decision"] == "pending_approval"

    def test_no_config_kwarg_holds_gate_closed(self, tmp_path):
        """Calling without the config kwarg (3-positional-arg form) keeps
        working — and now resolves to the gate-closed default (no bundle →
        NOT_ELIGIBLE, nothing written)."""
        pat = _eligible_pattern()
        result = promote_pattern_to_recipe(pat, "python", tmp_path / "staged")
        assert result == PromotionResult.NOT_ELIGIBLE
        assert not (tmp_path / "staged").exists() or list(
            (tmp_path / "staged").glob("*.yaml")
        ) == []

    def test_explicit_gate_open_config_still_promotes(self, tmp_path):
        """Explicit operator config overrides the gate-closed default."""
        pat = _eligible_pattern()
        staged = tmp_path / "staged"
        result = promote_pattern_to_recipe(
            pat, "python", staged, config=_gate_open_config()
        )
        assert result == PromotionResult.PROMOTED
        assert len(list(staged.glob("*.yaml"))) == 1

    def test_no_audit_path_no_crash(self, tmp_path):
        """``approval_records_path=None`` → no audit record, YAML still written."""
        pat = _eligible_pattern()
        result = promote_pattern_to_recipe(
            pat,
            "python",
            tmp_path / "staged",
            config=_gate_open_config(),
            approval_records_path=None,
        )
        assert result == PromotionResult.PROMOTED
        assert len(list((tmp_path / "staged").glob("*.yaml"))) == 1


class TestPausedHardHold:
    """C2 (ADR-0013): learning.mode=paused is a HARD HOLD at the runtime
    promotion gate — stronger than gate_closed: no YAML, no pending record,
    even when the per-class config explicitly says gate_open_audit."""

    def _paused_config(self):
        return {
            "learning": {
                "mode": "paused",
                # Explicit gate-open per-class setting must NOT punch through
                # the paused ceiling.
                "asset_classes": {
                    "pattern": {"write_producing": "gate_open_audit"},
                },
            },
        }

    def test_paused_writes_nothing_and_records_nothing(self, tmp_path):
        pat = _eligible_pattern()
        asset_ref = f"pattern:{pat.pattern_id}"
        _write_bundle(tmp_path / "state", asset_ref)  # bundle present; must not matter
        staged = tmp_path / "staged"
        records_path = tmp_path / "records.jsonl"

        result = promote_pattern_to_recipe(
            pat,
            "python",
            staged,
            config=self._paused_config(),
            repo_state_dir=tmp_path / "state",
            approval_records_path=records_path,
        )
        assert result == PromotionResult.NOT_ELIGIBLE  # held (NOT_ELIGIBLE-style return)
        assert not staged.exists() or list(staged.glob("*.yaml")) == []
        # Hard hold: no pending_approval AND no auto_promote record.
        assert read_jsonl(records_path) == []

    def test_audit_only_still_behaves_gate_closed(self, tmp_path):
        """Pin: audit_only forces gate-closed at this gate (the pre-existing
        ceiling) — with a bundle it queues pending approval; paused is
        strictly stronger (writes nothing, queues nothing)."""
        pat = _eligible_pattern()
        _write_bundle(tmp_path / "state", f"pattern:{pat.pattern_id}")
        staged = tmp_path / "staged"
        records_path = tmp_path / "records.jsonl"

        result = promote_pattern_to_recipe(
            pat,
            "python",
            staged,
            config={"learning": {"mode": "audit_only"}},
            repo_state_dir=tmp_path / "state",
            approval_records_path=records_path,
        )
        assert result == PromotionResult.PENDING_APPROVAL
        assert not staged.exists() or list(staged.glob("*.yaml")) == []
        records = read_jsonl(records_path)
        assert len(records) == 1
        assert records[0]["decision"] == "pending_approval"
