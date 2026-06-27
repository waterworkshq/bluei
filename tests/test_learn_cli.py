"""Tests for bin.cmd_learn — Phase 8 of alpha.2 (PROMPT-09).

Covers inbox, status, audit, bundle subcommands. All tests run with a
temporary repos state root — no real ConfigManager / registry touched.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, ".")

from bin.bluei import _cmd_learn  # noqa: E402
from bin.cmd_learn import (  # noqa: E402
    _apply_before_after,
    _derive_detector_command,
    _learn_audit,
    _learn_bundle,
    _learn_inbox,
    _learn_status,
)
from bluei.engine.bundle_loader import GoldenBundle, load_bundles  # noqa: E402
from bluei.engine.pattern_store import FixPattern, FixPatternStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def _approval_record(
    *,
    asset_ref: str,
    decision: str,
    timestamp: str,
    reason: str = "",
    actor: str = "",
    native_state_before: str = "active",
    native_state_after: str = "active",
    evidence_snapshot: dict | None = None,
) -> dict:
    return {
        "asset_ref": asset_ref,
        "decision": decision,
        "native_state_before": native_state_before,
        "native_state_after": native_state_after,
        "reason": reason,
        "evidence_snapshot": evidence_snapshot or {},
        "actor": actor,
        "timestamp": timestamp,
    }


def _dry_replay_record(
    *,
    pattern_id: str,
    outcome: str,
    timestamp: str,
    finding_id: str = "f1",
    run_id: str = "r1",
    rule: str = "ruff-b904",
) -> dict:
    return {
        "finding_id": finding_id,
        "pattern_id": pattern_id,
        "rule": rule,
        "run_id": run_id,
        "timestamp": timestamp,
        "validation_commands_passed": [],
        "would_have_outcome": outcome,
    }


def _make_pattern(
    *,
    pattern_id: str = "fp-test1234567",
    rule: str = "ruff-b904",
    before_snippet: str = "except:\n    pass",
    after_snippet: str = "except Exception:\n    pass",
    **overrides,
) -> FixPattern:
    data = {
        "pattern_id": pattern_id,
        "rule": rule,
        "language": "python",
        "file_path": "src/app.py",
        "before_snippet": before_snippet,
        "after_snippet": after_snippet,
        "diff_patch": "",
        "confidence": 0.8,
        "success_count": 5,
        "failure_count": 1,
        "skip_count": 0,
        "source": "claude",
        "created_at": "2026-05-17T10:30:00+00:00",
        "last_used_at": "2026-05-18T14:22:00+00:00",
        "source_finding_ids": ["f-001"],
    }
    data.update(overrides)
    return FixPattern(**data)


@pytest.fixture
def state_env(tmp_path: Path):
    """Build a repos state root and return (state_dir, repos_dir, repo_name)."""
    repo_name = "myrepo"
    repos_dir = tmp_path / "repos"
    state_dir = repos_dir / repo_name / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir, repos_dir, repo_name


# ---------------------------------------------------------------------------
# _derive_detector_command + _apply_before_after unit tests
# ---------------------------------------------------------------------------


class TestDetectorDerivation:
    def test_ruff_prefix_maps_to_ruff_select(self):
        argv = _derive_detector_command("ruff-b904")
        assert argv == [
            "ruff",
            "check",
            "--select",
            "B904",
            "--output-format=concise",
            "-",
        ]

    def test_ruff_prefix_uppercases_code(self):
        argv = _derive_detector_command("ruff-c408")
        assert "C408" in argv

    def test_unknown_rule_returns_none(self):
        assert _derive_detector_command("custom-rule") is None
        assert _derive_detector_command("trailing-whitespace") is None


class TestApplyBeforeAfter:
    def test_literal_replace_first_occurrence(self):
        result = _apply_before_after(
            "before:\nexcept:\n    pass\nafter",
            "except:\n    pass",
            "except Exception:\n    pass",
        )
        assert "except Exception:\n    pass" in result
        assert result.count("except:") == 0

    def test_returns_none_when_snippet_missing(self):
        assert _apply_before_after("nothing here", "missing", "x") is None

    def test_normalizes_indentation(self):
        # Indentation differs but normalized matcher should still hit.
        result = _apply_before_after(
            "    except:\n        pass\n",
            "except:\n    pass",
            "except Exception:\n    pass",
        )
        assert result is not None
        assert "except Exception:" in result


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


class TestInbox:
    def test_inbox_empty_when_no_records(self, state_env, capsys):
        state_dir, _, repo = state_env
        rc = _learn_inbox(repo, state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No pending approvals." in out

    def test_inbox_empty_when_file_missing(self, state_env, capsys):
        state_dir, _, repo = state_env
        # No approval_records.jsonl written at all.
        rc = _learn_inbox(repo, state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No pending approvals." in out

    def test_inbox_with_pending(self, state_env, capsys):
        state_dir, _, repo = state_env
        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref="pattern:fp-aaa",
                    decision="pending_approval",
                    timestamp="2026-06-27T10:00:00Z",
                    reason="needs review",
                ),
                _approval_record(
                    asset_ref="pattern:fp-bbb",
                    decision="pending_approval",
                    timestamp="2026-06-27T11:00:00Z",
                    reason="second one",
                ),
            ],
        )
        rc = _learn_inbox(repo, state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "pattern:fp-aaa" in out
        assert "pattern:fp-bbb" in out
        assert "needs review" in out
        assert "2" in out  # count

    def test_inbox_hides_non_pending(self, state_env, capsys):
        state_dir, _, repo = state_env
        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref="pattern:fp-pending",
                    decision="pending_approval",
                    timestamp="2026-06-27T10:00:00Z",
                    reason="waiting",
                ),
                _approval_record(
                    asset_ref="pattern:fp-approved",
                    decision="approve",
                    timestamp="2026-06-27T11:00:00Z",
                ),
                _approval_record(
                    asset_ref="pattern:fp-rejected",
                    decision="reject",
                    timestamp="2026-06-27T12:00:00Z",
                ),
            ],
        )
        rc = _learn_inbox(repo, state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "pattern:fp-pending" in out
        assert "pattern:fp-approved" not in out
        assert "pattern:fp-rejected" not in out

    def test_inbox_after_approve_is_empty(self, state_env, capsys):
        """A pending record followed by an approve should yield an empty inbox."""
        state_dir, _, repo = state_env
        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref="pattern:fp-xyz",
                    decision="pending_approval",
                    timestamp="2026-06-27T10:00:00Z",
                ),
                _approval_record(
                    asset_ref="pattern:fp-xyz",
                    decision="approve",
                    timestamp="2026-06-27T11:00:00Z",
                    actor="operator:alice",
                ),
            ],
        )
        rc = _learn_inbox(repo, state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No pending approvals." in out
        assert "pattern:fp-xyz" not in out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_with_no_records(self, state_env, capsys):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        state = StateManager(repos_dir)
        rc = _learn_status("pattern:fp-abc", repo, state_dir, state)
        assert rc == 0
        out = capsys.readouterr().out
        assert "asset_ref:        pattern:fp-abc" in out
        assert "governance_state: active" in out  # default active
        # No pattern in store → not found message
        assert "(pattern not found in store)" in out
        assert "(no dry-replay records)" in out

    def test_status_with_governance_and_pattern(self, state_env, capsys):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref="pattern:fp-aaa",
                    decision="pause",
                    timestamp="2026-06-27T10:00:00Z",
                    reason="manual",
                ),
            ],
        )
        # Write the pattern into the store. The store assigns its own id, so
        # we capture the returned id and rewrite the approval record to match.
        store_path = state_dir / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(
            pattern_id="",
            confidence=0.42,
            success_count=3,
            failure_count=2,
            skip_count=1,
        )
        actual_pid = store.append(pattern)

        # Realign the approval record so the asset_ref resolves to the stored id.
        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref=f"pattern:{actual_pid}",
                    decision="pause",
                    timestamp="2026-06-27T10:00:00Z",
                    reason="manual",
                ),
            ],
        )

        state = StateManager(repos_dir)
        rc = _learn_status(f"pattern:{actual_pid}", repo, state_dir, state)
        assert rc == 0
        out = capsys.readouterr().out
        assert "governance_state: paused" in out
        assert "confidence=0.420" in out
        assert "HIT=3" in out
        assert "MISS=1" in out
        assert "FAILURE=2" in out

    def test_status_with_dry_replay_evidence(self, state_env, capsys):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        _write_jsonl(
            state_dir / "dry_replay.jsonl",
            [
                _dry_replay_record(
                    pattern_id="fp-aaa",
                    outcome="HIT",
                    timestamp="2026-06-27T10:00:00Z",
                ),
                _dry_replay_record(
                    pattern_id="fp-aaa",
                    outcome="FAILURE",
                    timestamp="2026-06-27T11:00:00Z",
                ),
                # unrelated pattern — should be excluded
                _dry_replay_record(
                    pattern_id="fp-bbb",
                    outcome="HIT",
                    timestamp="2026-06-27T12:00:00Z",
                ),
            ],
        )
        state = StateManager(repos_dir)
        rc = _learn_status("pattern:fp-aaa", repo, state_dir, state)
        assert rc == 0
        out = capsys.readouterr().out
        assert "HIT=1" in out
        assert "FAILURE=1" in out


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_no_history(self, state_env, capsys):
        state_dir, _repos_dir, _repo = state_env
        rc = _learn_audit("pattern:fp-abc", state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No decision history" in out

    def test_audit_full_history_chronological(self, state_env, capsys):
        state_dir, _repos_dir, _repo = state_env
        records = [
            _approval_record(
                asset_ref="pattern:fp-aaa",
                decision="pending_approval",
                timestamp="2026-06-27T10:00:00Z",
                actor="system:promotion_gate",
                reason="threshold crossed",
            ),
            _approval_record(
                asset_ref="pattern:fp-aaa",
                decision="approve",
                timestamp="2026-06-27T11:00:00Z",
                actor="operator:bob",
                reason="LGTM",
            ),
            _approval_record(
                asset_ref="pattern:fp-aaa",
                decision="pause",
                timestamp="2026-06-27T12:00:00Z",
                actor="operator:bob",
                reason="investigating regression",
            ),
            # unrelated asset
            _approval_record(
                asset_ref="pattern:fp-bbb",
                decision="approve",
                timestamp="2026-06-27T13:00:00Z",
            ),
        ]
        _write_jsonl(state_dir / "approval_records.jsonl", records)

        rc = _learn_audit("pattern:fp-aaa", state_dir)
        assert rc == 0
        out = capsys.readouterr().out
        assert "3 records" in out
        # All three decisions appear in chronological order.
        decisions_in_output = [
            line.split("decision: ")[1]
            for line in out.splitlines()
            if "decision: " in line
        ]
        assert decisions_in_output == [
            "pending_approval",
            "approve",
            "pause",
        ]
        # The unrelated record is filtered out.
        assert "pattern:fp-bbb" not in out
        # Actor + reason surfaced.
        assert "operator:bob" in out
        assert "investigating regression" in out


# ---------------------------------------------------------------------------
# bundle
# ---------------------------------------------------------------------------


class TestBundle:
    def test_bundle_creates_yaml_with_correct_fields(self, state_env, tmp_path, capsys):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        # Write the pattern store. FixPatternStore.append assigns its own id;
        # capture it and reference it everywhere below.
        store_path = state_dir / "fix_patterns.jsonl"
        store = FixPatternStore(store_path)
        pattern = _make_pattern(
            pattern_id="",
            rule="ruff-b904",
            before_snippet="except:\n    pass",
            after_snippet="except Exception:\n    pass",
            file_path="src/app.py",
        )
        pid = store.append(pattern)

        # Write a finding pointing at src/app.py.
        findings_path = state_dir / "findings.jsonl"
        _write_jsonl(
            findings_path,
            [
                {
                    "finding_id": "f-001",
                    "repo": repo,
                    "path": "src/app.py",
                    "line": 5,
                    "rule": "ruff-b904",
                    "snippet": "except:\n    pass",
                    "confidence": 0.8,
                    "quick_win": True,
                    "safe_to_autofix": True,
                }
            ],
        )

        # Build a worktree with the target file containing the before snippet.
        # The `pass` is indented one level deeper than `except` so the
        # normalized-snippet matcher (which collapses whitespace runs) hits.
        worktree = tmp_path / "worktree"
        (worktree / "src").mkdir(parents=True, exist_ok=True)
        (worktree / "src" / "app.py").write_text(
            "def f():\n    try:\n        do_thing()\n    except:\n        pass\n",
            encoding="utf-8",
        )

        state = StateManager(repos_dir)
        rc = _learn_bundle(pid, worktree, "f-001", repo, state_dir, state)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Bundle written" in out

        bundle_path = state_dir / "golden_bundles" / f"gb-{pid}-f-001.yaml"
        assert bundle_path.exists()

        data = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
        assert data["id"] == f"gb-{pid}-f-001"
        assert data["asset_class"] == "pattern"
        assert data["asset_ref"] == f"pattern:{pid}"
        assert data["rule"] == "ruff-b904"
        assert data["rule_family"] == "ruff"
        assert data["language"] == "python"
        assert "except:" in data["before"]
        assert "except Exception:" in data["after"]
        assert data["source_finding_id"] == "f-001"
        assert data["extracted_at"]
        # validation_command is the ruff argv as a string.
        assert "ruff" in data["validation_command"]
        assert "B904" in data["validation_command"]
        # detector_before / detector_after may be empty if ruff is missing —
        # but they must always be present as keys.
        assert "detector_before" in data
        assert "detector_after" in data

        # The bundle should round-trip through GoldenBundle.from_dict.
        bundle = GoldenBundle.from_dict(data)
        assert bundle.id == f"gb-{pid}-f-001"
        assert bundle.asset_ref == f"pattern:{pid}"

    def test_bundle_unknown_rule_leaves_detector_empty(
        self, state_env, tmp_path, capsys
    ):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        store = FixPatternStore(state_dir / "fix_patterns.jsonl")
        pid = store.append(
            _make_pattern(
                pattern_id="",
                rule="custom-rule",  # no detector mapping
                before_snippet="TODO old\n",
                after_snippet="TODO new\n",
                file_path="src/note.txt",
            )
        )

        _write_jsonl(
            state_dir / "findings.jsonl",
            [
                {
                    "finding_id": "f-002",
                    "repo": repo,
                    "path": "src/note.txt",
                    "line": 1,
                    "rule": "custom-rule",
                    "snippet": "TODO old",
                    "confidence": 0.5,
                    "quick_win": False,
                    "safe_to_autofix": False,
                }
            ],
        )

        worktree = tmp_path / "wt"
        (worktree / "src").mkdir(parents=True)
        (worktree / "src" / "note.txt").write_text(
            "header\nTODO old\nfooter\n", encoding="utf-8"
        )

        state = StateManager(repos_dir)
        rc = _learn_bundle(pid, worktree, "f-002", repo, state_dir, state)
        assert rc == 0

        data = yaml.safe_load(
            (state_dir / "golden_bundles" / f"gb-{pid}-f-002.yaml").read_text(
                encoding="utf-8"
            )
        )
        assert data["validation_command"] == ""
        assert data["detector_before"] == ""
        assert data["detector_after"] == ""
        assert "TODO old" in data["before"]
        assert "TODO new" in data["after"]

    def test_bundle_missing_pattern_returns_error(self, state_env, tmp_path, capsys):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        state = StateManager(repos_dir)
        rc = _learn_bundle("fp-nope", tmp_path, "f-001", repo, state_dir, state)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_bundle_missing_finding_returns_error(self, state_env, tmp_path, capsys):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        store = FixPatternStore(state_dir / "fix_patterns.jsonl")
        pid = store.append(_make_pattern(pattern_id=""))

        state = StateManager(repos_dir)
        rc = _learn_bundle(pid, tmp_path, "f-nope", repo, state_dir, state)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_bundle_missing_file_in_worktree_returns_error(
        self, state_env, tmp_path, capsys
    ):
        state_dir, repos_dir, repo = state_env
        from bluei.app.state import StateManager

        store = FixPatternStore(state_dir / "fix_patterns.jsonl")
        pid = store.append(_make_pattern(pattern_id="", file_path="src/missing.py"))

        _write_jsonl(
            state_dir / "findings.jsonl",
            [
                {
                    "finding_id": "f-001",
                    "repo": repo,
                    "path": "src/missing.py",
                    "line": 1,
                    "rule": "ruff-b904",
                    "snippet": "x",
                    "confidence": 0.5,
                    "quick_win": False,
                    "safe_to_autofix": False,
                }
            ],
        )

        state = StateManager(repos_dir)
        rc = _learn_bundle(pid, tmp_path, "f-001", repo, state_dir, state)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err


# ---------------------------------------------------------------------------
# Dispatcher (_cmd_learn) end-to-end via the CLI entrypoint
# ---------------------------------------------------------------------------


class TestCmdLearnDispatch:
    def test_no_args_shows_help(self, capsys):
        rc = _cmd_learn([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "bluei learn" in out

    def test_help_flag_shows_help(self, capsys):
        rc = _cmd_learn(["--help"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "bluei learn" in out

    def test_unknown_subcommand_returns_error(self, capsys):
        rc = _cmd_learn(["bogus", "--repo", "r"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "unknown learn subcommand" in err

    def test_missing_repo_returns_error(self, capsys):
        rc = _cmd_learn(["inbox"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--repo" in err

    def test_status_requires_asset_ref(self, capsys):
        rc = _cmd_learn(["status", "--repo", "r"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "asset_ref" in err

    def test_audit_requires_asset_ref(self, capsys):
        rc = _cmd_learn(["audit", "--repo", "r"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "asset_ref" in err

    def test_bundle_requires_positional_pattern_id(self, capsys):
        rc = _cmd_learn(["bundle", "--repo", "r"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "pattern_id" in err

    def test_bundle_requires_worktree_and_finding(self, capsys):
        rc = _cmd_learn(["bundle", "fp-aaa", "--repo", "r"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "--worktree" in err

    def test_dispatch_inbox_via_state_root(self, tmp_path, capsys):
        """End-to-end: drive _cmd_learn with --state-root and verify it reads
        the synthetic approval_records.jsonl."""
        repo_name = "myrepo"
        repos_dir = tmp_path / "repos"
        state_dir = repos_dir / repo_name / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref="pattern:fp-aaa",
                    decision="pending_approval",
                    timestamp="2026-06-27T10:00:00Z",
                    reason="via dispatch",
                ),
            ],
        )
        rc = _cmd_learn(["inbox", "--repo", repo_name, "--state-root", str(repos_dir)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pattern:fp-aaa" in out
        assert "via dispatch" in out

    def test_dispatch_audit_via_state_root(self, tmp_path, capsys):
        repo_name = "myrepo"
        repos_dir = tmp_path / "repos"
        state_dir = repos_dir / repo_name / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(
            state_dir / "approval_records.jsonl",
            [
                _approval_record(
                    asset_ref="pattern:fp-aaa",
                    decision="approve",
                    timestamp="2026-06-27T10:00:00Z",
                    actor="operator:dispatch",
                    reason="dispatch test",
                ),
            ],
        )
        rc = _cmd_learn(
            [
                "audit",
                "pattern:fp-aaa",
                "--repo",
                repo_name,
                "--state-root",
                str(repos_dir),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "operator:dispatch" in out
        assert "dispatch test" in out
