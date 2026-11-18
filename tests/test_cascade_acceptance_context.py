"""Cascade lane acceptance-context regressions (LANE-ACCEPTANCE-zhy98vs7, Stage 3).

Covers the configured-context contract added to ``apply_cascade_fix``:

- tier validation receives the caller's configured checks and environment
  repo instead of validating against nothing (A03);
- the model fallback runs ONLY with a configured command template and
  carries the caller's baseline checks plus private baseline statuses —
  the built-in default template and sample baseline checks are never
  substituted (A10);
- ``deterministic_only`` blocks the model fallback regardless of template;
- deferred-acceptance mode persists NO positive outcome (ledger success,
  pattern extraction, replay HIT, savings) before the caller's outer
  durable acceptance invokes ``record["confirm"]()``, which runs each
  deferred effect exactly once;
- replay/composite cascade stages defer positive evidence at stage level
  while negative evidence stays immediate.

All checks are local: no network, no gh, no real model CLI (the fallback
engine is monkeypatched); tier stubs avoid executing real commands except
where an explicit ``true`` control is asserted.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bluei.engine.cascade import (
    CascadeContext,
    CascadeResult,
    CascadeStage,
    CompositePatternCascadeStage,
    PatternReplayCascadeStage,
)
from bluei.engine.fix_tiers import FixTier, TieredValidationResult, TieredValidator
from bluei.engine import lifecycle


@pytest.fixture(autouse=True)
def _hermetic_checkpoint_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUEI_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))


class _SucceedStage(CascadeStage):
    """Deterministic stage that edits the finding target and succeeds."""

    name = "always-succeed"
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        target = worktree / finding.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixed = dict(a=1)\n")
        return CascadeResult(
            success=True,
            stage_name=self.name,
            changes_made=[finding.path],
            validation_passed=True,
        )


class _FailStage(CascadeStage):
    """Deterministic stage that never succeeds."""

    name = "always-fail"
    tier = 1

    def can_handle(self, finding, context):
        return True

    def attempt(self, finding, worktree, context):
        return CascadeResult(success=False, stage_name=self.name, error="nope")


@pytest.fixture
def tier_stub(monkeypatch):
    """Replace tier validation with a passing stub that records its kwargs."""
    calls = []

    class _Validator:
        def validate(self, tier, worktree_path, finding, **kwargs):
            calls.append({"tier": tier, "kwargs": kwargs})
            return TieredValidationResult(passed=True, tier=tier)

    monkeypatch.setattr(lifecycle, "_get_tiered_validator", _Validator)
    return calls


def _run(worktree, finding, log, **kwargs):
    return lifecycle.apply_cascade_fix(worktree, finding, log, **kwargs)


# ── configured checks reach the tier gate (A03) ────────────────────────────


def test_tier_validation_receives_configured_checks_and_environment(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_SucceedStage()]
    )
    log = git_repo / "log.txt"
    log.write_text("")
    source = git_repo.parent / "source"
    source.mkdir()

    assert _run(
        git_repo,
        make_finding(),
        log,
        baseline_checks={"unit": ["true"]},
        baseline_results={"unit": {"rc": 0, "available": True}},
        environment_repo=source,
    ) is True

    assert len(tier_stub) == 1
    assert tier_stub[0]["kwargs"]["checks"] == {"unit": ["true"]}
    assert tier_stub[0]["kwargs"]["environment_repo"] == source


def test_tier_validation_without_context_keeps_legacy_none_checks(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_SucceedStage()]
    )
    log = git_repo / "log.txt"
    log.write_text("")

    assert _run(git_repo, make_finding(), log) is True
    assert tier_stub[0]["kwargs"]["checks"] is None
    assert tier_stub[0]["kwargs"]["environment_repo"] is None


def test_t1_runs_configured_checks_with_environment_repo(monkeypatch, tmp_path):
    """T1 executes the configured commands with the source runtime origin."""
    from bluei.engine import validation as validation_mod

    captured = {}

    def fake_run_named_checks(**kwargs):
        captured.update(kwargs)
        return {"unit": {"rc": 0, "available": True}}

    monkeypatch.setattr(validation_mod, "run_named_checks", fake_run_named_checks)
    source = tmp_path / "source"
    result = TieredValidator().validate(
        FixTier.T1_IDEMPOTENT,
        tmp_path,
        SimpleNamespace(path="src/main.py"),
        checks={"unit": ["true"]},
        log_file=tmp_path / "log.txt",
        environment_repo=source,
    )
    assert result.passed is True
    assert captured["checks"] == {"unit": ["true"]}
    assert captured["environment_repo"] == source


# ── model fallback uses configured evidence only (A10) ────────────────────


def test_model_fallback_refuses_without_configured_template(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    explode = MagicMock(side_effect=AssertionError("model engine must not run"))
    monkeypatch.setattr(lifecycle, "apply_claude_fix", explode)
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []

    # Direct legacy caller with NO configured context:
    assert _run(git_repo, make_finding(), log, ledger_records=ledger) is False
    # Caller with checks but no template — still no fabricated fallback:
    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            baseline_checks={"unit": ["true"]},
        )
        is False
    )
    explode.assert_not_called()
    assert all(record["outcome"] == "exhausted" for record in ledger)
    assert "model fallback unqualified" in log.read_text()


def test_model_fallback_uses_configured_template_and_private_statuses(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    requests = []
    monkeypatch.setattr(
        lifecycle,
        "apply_claude_fix",
        lambda req: (requests.append(req) or (0, "ok", "/tmp/prompt")),
    )
    log = git_repo / "log.txt"
    log.write_text("")

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            baseline_checks={"unit": ["true"]},
            baseline_results={
                "unit": {"rc": 0, "available": True},
                "lint": {"rc": 1, "available": True},
            },
            allow_unchanged_baseline_failures=False,
        )
        is True
    )

    assert len(requests) == 1
    req = requests[0]
    assert req.claude_cmd_template == "opencode run {prompt_file}"
    assert req.baseline_checks == {"unit": ["true"]}
    assert req.baseline_statuses == [0, 1]
    assert req.allow_unchanged_baseline_failures is False


def test_deterministic_only_blocks_model_fallback(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    explode = MagicMock(side_effect=AssertionError("model engine must not run"))
    monkeypatch.setattr(lifecycle, "apply_claude_fix", explode)
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            deterministic_only=True,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            ledger_records=ledger,
        )
        is False
    )
    explode.assert_not_called()
    assert ledger[-1]["outcome"] == "exhausted"


# ── deferred acceptance: nothing positive before outer confirm ─────────────


def test_deferred_deterministic_candidate_holds_all_positive_effects(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_SucceedStage()]
    )
    extract = MagicMock()
    monkeypatch.setattr(lifecycle, "_extract_fix_pattern", extract)
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            pattern_store_path=git_repo.parent / "state" / "patterns.jsonl",
            ledger_records=ledger,
            deferred_acceptance=deferred,
            baseline_checks={"unit": ["true"]},
        )
        is True
    )

    # Candidate delivered, nothing positive persisted yet.
    assert len(deferred) == 1
    record = deferred[0]
    assert record["kind"] == "cascade-candidate"
    assert record["outcome"] is None
    assert record["origin"] == "deterministic"
    assert record["stage_name"] == "always-succeed"
    assert record["model_attempt"] is False
    assert record["deterministic_attempt"] is True
    assert record["model_invocations"] == 0
    assert record["deterministic_invocations"] == 1  # the winning stage ran
    assert record["model_request"] is None
    assert record["qualified"] is True
    assert ledger == []
    extract.assert_not_called()

    # Outer durable acceptance: confirm runs every deferred effect once.
    record["confirm"]()
    extract.assert_called_once()
    assert [r["outcome"] for r in ledger] == ["resolved_deterministic"]
    record["confirm"]()  # one-shot: no duplicate ledger emission
    assert len(ledger) == 1


def test_deferred_llm_candidate_records_attempts_and_confirms_once(
    git_repo, make_finding, tier_stub, monkeypatch
):
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    monkeypatch.setattr(
        lifecycle, "apply_claude_fix", lambda req: (0, "ok", "/tmp/prompt")
    )
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
        )
        is True
    )

    record = deferred[0]
    assert record["origin"] == "llm"
    assert record["backend"] == "opencode"
    assert record["model_attempt"] is True
    assert record["deterministic_attempt"] is True  # stages ran and failed first
    assert record["model_invocations"] == 1  # the one successful model call
    assert record["deterministic_invocations"] == 1  # failed stage counted too
    assert record["model_request"] == {
        "backend": "opencode",
        "claude_cmd_template": "opencode run {prompt_file}",
    }
    assert record["qualified"] is False  # no configured checks supplied
    assert ledger == []

    record["confirm"]()
    assert [r["outcome"] for r in ledger] == ["resolved_llm"]
    record["confirm"]()
    assert len(ledger) == 1


def test_deferred_rejection_stays_immediate_and_honest(
    git_repo, make_finding, monkeypatch
):
    """A tier rejection in defer mode records exhausted now, plus one
    attempt record carrying the actual invocation counters (never a
    candidate and never any positive confirmation)."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_SucceedStage()]
    )

    class _RejectValidator:
        def validate(self, tier, worktree_path, finding, **kwargs):
            return TieredValidationResult(
                passed=False, tier=tier, message="configured check failed"
            )

    monkeypatch.setattr(lifecycle, "_get_tiered_validator", _RejectValidator)
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            deferred_acceptance=deferred,
            baseline_checks={"unit": ["false"]},
        )
        is False
    )
    # One honest attempt record — counters include the rejected stage run.
    assert len(deferred) == 1
    attempt = deferred[0]
    assert attempt["kind"] == "cascade-attempt"
    assert attempt["outcome"] == "rejected"
    assert attempt["origin"] == "deterministic"
    assert attempt["model_invocations"] == 0
    assert attempt["deterministic_invocations"] == 1
    assert attempt["model_request"] is None
    # Negative ledger evidence is immediate (not gated on confirm()).
    assert [r["outcome"] for r in ledger] == ["exhausted"]
    # Attempt confirm is a one-shot no-op: no new ledger rows.
    attempt["confirm"]()
    attempt["confirm"]()
    assert [r["outcome"] for r in ledger] == ["exhausted"]
    # Rejection restored the pre-attempt worktree state.
    assert not (git_repo / "src" / "main.py").exists()


# ── stage-level deferral of positive evidence ──────────────────────────────


def test_replay_stage_defers_hit_and_savings(tmp_path, make_finding, monkeypatch):
    from bluei.engine import pattern_replay

    captured = {}

    def fake_try_replay(*args, **kwargs):
        captured.update(kwargs)
        return True, "pid-1"

    monkeypatch.setattr(pattern_replay, "try_replay", fake_try_replay)

    store = MagicMock()
    tracker = MagicMock()
    tracker.estimate_invocation_cost.return_value = 0.01
    log = tmp_path / "log.txt"
    log.write_text("")
    context = CascadeContext(
        worktree=tmp_path,
        log_file=log,
        defer_success_evidence=True,
        pattern_store=store,
        cost_tracker=tracker,
    )

    result = PatternReplayCascadeStage().attempt(
        make_finding(), tmp_path, context
    )

    assert result.success is True
    assert captured["record_success"] is False
    store.record_replay_outcome.assert_not_called()
    tracker.record_pattern_replay_savings.assert_not_called()

    for confirmation in result.pending_confirmations:
        confirmation()
    store.record_replay_outcome.assert_called_once()
    tracker.record_pattern_replay_savings.assert_called_once()


def test_replay_stage_legacy_records_success_eagerly(
    tmp_path, make_finding, monkeypatch
):
    from bluei.engine import pattern_replay

    monkeypatch.setattr(
        pattern_replay, "try_replay", lambda *a, **k: (True, "pid-2")
    )
    store = MagicMock()
    tracker = MagicMock()
    tracker.estimate_invocation_cost.return_value = 0.01
    context = CascadeContext(
        worktree=tmp_path,
        log_file=tmp_path / "log.txt",
        pattern_store=store,
        cost_tracker=tracker,
    )

    result = PatternReplayCascadeStage().attempt(
        make_finding(), tmp_path, context
    )

    assert result.success is True
    assert result.pending_confirmations == []
    tracker.record_pattern_replay_savings.assert_called_once()


def test_composite_stage_defers_success_count_and_savings(
    tmp_path, make_finding, monkeypatch
):
    from bluei.engine import composite_pattern, validation as validation_mod

    comp_store = MagicMock()
    comp_store.lookup.return_value = SimpleNamespace(
        pattern_id="cp-1", confidence=0.9, steps=[]
    )
    monkeypatch.setattr(
        composite_pattern, "CompositePatternStore", lambda path: comp_store
    )
    applier = MagicMock()
    applier.apply.return_value = (True, ["src/main.py"], None)
    monkeypatch.setattr(
        composite_pattern, "CompositePatternApplier", lambda: applier
    )
    monkeypatch.setattr(
        validation_mod, "verify_fix_closed", lambda *a, **k: True
    )

    pattern_store = MagicMock()
    pattern_store.store_path = tmp_path / "patterns.jsonl"
    tracker = MagicMock()
    tracker.estimate_invocation_cost.return_value = 0.02
    context = CascadeContext(
        worktree=tmp_path,
        log_file=tmp_path / "log.txt",
        defer_success_evidence=True,
        pattern_store=pattern_store,
        cost_tracker=tracker,
    )

    result = CompositePatternCascadeStage().attempt(
        make_finding(), tmp_path, context
    )

    assert result.success is True
    comp_store.record_result.assert_not_called()
    tracker.record_pattern_replay_savings.assert_not_called()

    for confirmation in result.pending_confirmations:
        confirmation()
    comp_store.record_result.assert_called_once_with("cp-1", True)
    tracker.record_pattern_replay_savings.assert_called_once()


# ── failure-path attempt records: actual invocation counters ──────────────


def test_deferred_model_failure_counts_actual_invocation(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """A FAILED model engine call must not be hidden: the attempt record
    carries model_invocations=1 with the request identity and outcome."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    monkeypatch.setattr(
        lifecycle, "apply_claude_fix", lambda req: (1, "boom", "/tmp/p")
    )
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            baseline_checks={"unit": ["true"]},
        )
        is False
    )

    assert len(deferred) == 1
    attempt = deferred[0]
    assert attempt["kind"] == "cascade-attempt"
    assert attempt["outcome"] == "model-failed"
    assert attempt["origin"] == "llm"
    assert attempt["model_attempt"] is True
    assert attempt["model_invocations"] == 1  # the failed call is counted
    assert attempt["deterministic_invocations"] == 1  # failed stage counted
    assert attempt["model_request"] == {
        "backend": "opencode",
        "claude_cmd_template": "opencode run {prompt_file}",
    }
    assert attempt["qualified"] is True
    assert [r["outcome"] for r in ledger] == ["exhausted"]


def test_deferred_deterministic_only_exhaustion_counts(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """deterministic_only exhaustion appends an honest attempt record:
    no model invocation, every tried stage counted (failures included)."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    explode = MagicMock(side_effect=AssertionError("model engine must not run"))
    monkeypatch.setattr(lifecycle, "apply_claude_fix", explode)
    log = git_repo / "log.txt"
    log.write_text("")
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            deterministic_only=True,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
        )
        is False
    )
    explode.assert_not_called()
    assert len(deferred) == 1
    attempt = deferred[0]
    assert attempt["kind"] == "cascade-attempt"
    assert attempt["outcome"] == "exhausted"
    assert attempt["model_invocations"] == 0
    assert attempt["model_request"] is None
    assert attempt["deterministic_invocations"] == 1


# ── no eager legacy success on production empty context ───────────────────


def test_eager_success_refused_on_production_empty_context(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """A caller that supplies acceptance kwargs but NO configured checks is
    a production lane: legacy eager success must not validate against
    nothing — fail closed before the tier gate, restore, honest exhausted."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_SucceedStage()]
    )
    extract = MagicMock()
    monkeypatch.setattr(lifecycle, "_extract_fix_pattern", extract)
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            claude_cmd_template="true {prompt_file}",
        )
        is False
    )
    assert tier_stub == []  # tier gate never ran with empty checks
    extract.assert_not_called()
    assert [r["outcome"] for r in ledger] == ["exhausted"]
    assert not (git_repo / "src" / "main.py").exists()  # restored
    assert "unqualified: acceptance context supplied without" in log.read_text()


def test_eager_model_refused_on_production_empty_context(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """Template without configured checks in eager mode: the model fallback
    refuses before invoking the engine (no production honesty bypass)."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    explode = MagicMock(side_effect=AssertionError("model engine must not run"))
    monkeypatch.setattr(lifecycle, "apply_claude_fix", explode)
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
        )
        is False
    )
    explode.assert_not_called()
    assert [r["outcome"] for r in ledger] == ["exhausted"]
    assert "production context without configured baseline checks" in log.read_text()


# ── G1: environment context reaches T2 check executions ───────────────────


def test_t2_threads_environment_repo_into_all_check_runs(tmp_path, monkeypatch):
    """T2 (and by forwarding T3/T4) executes baseline, post-fix and target
    checks with the caller's runtime origin instead of letting the gate
    silently re-run them in a worktree-local environment."""
    from bluei.engine import validation as validation_mod
    from bluei.engine import verify as verify_mod

    source = tmp_path / "source"
    runs = []

    def fake_run_named_checks(**kwargs):
        runs.append(kwargs)
        return {name: {"rc": 0, "available": True} for name in kwargs["checks"]}

    monkeypatch.setattr(validation_mod, "run_named_checks", fake_run_named_checks)
    forwarded = {}
    monkeypatch.setattr(
        verify_mod,
        "run_validation",
        lambda **kwargs: (
            forwarded.update(kwargs)
            or SimpleNamespace(passed=True, message="ok", regressions=[],
                               target_failures=[])
        ),
    )

    result = TieredValidator().validate(
        FixTier.T2_VALIDATED,
        tmp_path,
        SimpleNamespace(path="src/main.py"),
        checks={"unit": ["true"]},
        log_file=tmp_path / "log.txt",
        environment_repo=source,
    )

    assert result.passed is True
    phases = {r["phase"] for r in runs}
    assert phases == {
        "validation-t2-baseline",
        "validation-t2-postfix",
        "validation-t2-target",
    }
    assert all(r["environment_repo"] == source for r in runs)
    # The gate received the runtime-marked results; it never re-ran them.
    assert forwarded["baseline_results"] == {"unit": {"rc": 0, "available": True}}
    assert forwarded["post_fix_results"] == {"unit": {"rc": 0, "available": True}}
    assert forwarded["target_results"] == {"unit": {"rc": 0, "available": True}}


# ── cost guard, engine exceptions, and private request caps ───────────────


def test_cost_guard_skips_model_and_records_zero_invocations(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """A cost tracker at its hard limit blocks the model BEFORE any
    invocation: unqualified attempt with zero model counts and no ledger
    spend entry."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    explode = MagicMock(side_effect=AssertionError("model engine must not run"))
    monkeypatch.setattr(lifecycle, "apply_claude_fix", explode)
    tracker = MagicMock()
    tracker.exceeded_limit = lambda: True
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            baseline_checks={"unit": ["true"]},
            cost_tracker=tracker,
        )
        is False
    )
    explode.assert_not_called()
    tracker.record_invocation.assert_not_called()
    assert len(deferred) == 1
    attempt = deferred[0]
    assert attempt["kind"] == "cascade-attempt"
    assert attempt["outcome"] == "cost-limit"
    assert attempt["model_invocations"] == 0
    assert attempt["model_request"] is None
    assert [r["outcome"] for r in ledger] == ["exhausted"]


def test_model_engine_exception_counts_failed_invocation(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """An ordinary engine exception is an honest FAILED invocation:
    model_invocations=1 with the error category, backend-labeled nominal
    cost ledger entry, False return."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )

    def boom(req):
        raise RuntimeError("transport gone")

    monkeypatch.setattr(lifecycle, "apply_claude_fix", boom)
    tracker = MagicMock()
    tracker.exceeded_limit = lambda: False
    log = git_repo / "log.txt"
    log.write_text("")
    ledger = []
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            ledger_records=ledger,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            baseline_checks={"unit": ["true"]},
            cost_tracker=tracker,
        )
        is False
    )
    assert len(deferred) == 1
    attempt = deferred[0]
    assert attempt["kind"] == "cascade-attempt"
    assert attempt["outcome"] == "model-error"
    assert attempt["model_invocations"] == 1
    assert attempt["deterministic_invocations"] == 1
    assert attempt["error"] == "RuntimeError: transport gone"
    assert [r["outcome"] for r in ledger] == ["exhausted"]
    # Backend-labeled nominal estimate recorded — accounting, not a bill.
    tracker.record_invocation.assert_called_once_with(
        model="opencode", input_tokens=3000, output_tokens=300
    )


def test_model_interrupt_appends_attempt_then_reraises(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """BaseException from the engine: the launched invocation is accounted
    (attempt record + cost ledger) BEFORE the interrupt re-raises, so the
    caller's finally-side counters never lose it."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )

    def interrupted(req):
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "apply_claude_fix", interrupted)
    tracker = MagicMock()
    tracker.exceeded_limit = lambda: False
    log = git_repo / "log.txt"
    log.write_text("")
    deferred = []

    with pytest.raises(KeyboardInterrupt):
        _run(
            git_repo,
            make_finding(),
            log,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            baseline_checks={"unit": ["true"]},
            cost_tracker=tracker,
        )
    assert len(deferred) == 1
    attempt = deferred[0]
    assert attempt["kind"] == "cascade-attempt"
    assert attempt["outcome"] == "model-interrupted"
    assert attempt["model_invocations"] == 1
    tracker.record_invocation.assert_called_once_with(
        model="opencode", input_tokens=3000, output_tokens=300
    )


def test_caps_and_private_statuses_forwarded_to_model_request(
    git_repo, make_finding, tier_stub, monkeypatch
):
    """Caller-configured diff bounds reach the engine request, private
    baseline statuses ride alongside real checks, and the backend-labeled
    cost estimate is recorded exactly once on the actual invocation."""
    from bluei.engine import cascade as cascade_mod

    monkeypatch.setattr(
        cascade_mod, "default_cascade_stages", lambda: [_FailStage()]
    )
    requests = []

    def ok(req):
        requests.append(req)
        return (0, "ok", "/tmp/prompt")

    monkeypatch.setattr(lifecycle, "apply_claude_fix", ok)
    tracker = MagicMock()
    tracker.exceeded_limit = lambda: False
    log = git_repo / "log.txt"
    log.write_text("")
    deferred = []

    assert (
        _run(
            git_repo,
            make_finding(),
            log,
            deferred_acceptance=deferred,
            claude_cmd_template="opencode run {prompt_file}",
            backend="opencode",
            baseline_checks={"unit": ["true"], "lint": ["true"]},
            baseline_results={
                "unit": {"rc": 0, "available": True},
                "lint": {"rc": 1, "available": True},
            },
            cost_tracker=tracker,
            max_files_changed=7,
            max_loc_diff=99,
        )
        is True
    )
    req = requests[0]
    assert req.max_files_changed == 7
    assert req.max_loc_diff == 99
    assert req.claude_cmd_template == "opencode run {prompt_file}"
    assert req.baseline_statuses == [0, 1]  # private statuses, in check order
    assert req.baseline_checks == {"unit": ["true"], "lint": ["true"]}
    tracker.record_invocation.assert_called_once_with(
        model="opencode", input_tokens=3000, output_tokens=300
    )
