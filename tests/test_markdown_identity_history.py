"""Markdown legacy bare-rule (MD009) IDs must survive md-MD009 canonicalization."""

import json

from bluei.app.state import StateManager
from bluei.engine.models import Finding, now_iso
from bluei.engine.state import append_findings, filter_findings_by_cooldown
from plugins.markdown.plugin import stable_finding_id

REPO = "/tmp/example-repo"
PATH = "docs/readme.md"
DESC = "Trailing spaces [Expected: 0 or 2; Actual: 1]"


def _legacy_id(line: int) -> str:
    return stable_finding_id(REPO, PATH, line, "MD009", DESC)


def _legacy_record(line: int) -> dict:
    return {
        "finding_id": _legacy_id(line),
        "repo": REPO,
        "path": PATH,
        "line": line,
        "rule": "MD009",
        "snippet": DESC[:200],
        "confidence": 0.85,
        "quick_win": False,
        "safe_to_autofix": False,
        "severity": "low",
        "category": "docs",
        "discovered_at": "2026-10-01T00:00:00+00:00",
    }


def _current_finding(line: int) -> Finding:
    rule = "md-MD009"
    return Finding(
        finding_id=stable_finding_id(REPO, PATH, line, rule, DESC),
        repo=REPO,
        path=PATH,
        line=line,
        rule=rule,
        snippet=DESC[:200],
        confidence=0.85,
        quick_win=False,
        safe_to_autofix=False,
        severity="low",
        category="docs",
        discovered_at=now_iso(),
    )


def _write_jsonl(path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )


def test_append_adopts_legacy_id_and_preserves_jsonl(tmp_path):
    findings_file = tmp_path / "repos" / "demo" / "state" / "findings.jsonl"
    _write_jsonl(findings_file, [_legacy_record(3)])
    before = findings_file.read_text(encoding="utf-8")

    f3, f4 = _current_finding(3), _current_finding(4)
    assert append_findings(findings_file, [f3, f4]) == 1  # 3 adopted, 4 new
    assert f3.finding_id == _legacy_id(3)
    lines = findings_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0] == before.splitlines()[0]


def test_legacy_cooldown_applies_after_id_adoption(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    _write_jsonl(findings_file, [_legacy_record(3)])
    finding = _current_finding(3)
    append_findings(findings_file, [finding])

    state = {
        "finding_activity": {
            _legacy_id(3): {
                "last_action": "fix-failed",
                "last_action_at": now_iso(),
                "failure_count": 2,
            }
        }
    }
    allowed, suppressed = filter_findings_by_cooldown(
        [finding], state, 3600, tmp_path / "log.txt"
    )

    assert allowed == []
    assert [f.finding_id for f in suppressed] == [_legacy_id(3)]


def test_load_projection_collapses_aliases_keeps_distinct_lines(tmp_path):
    state = StateManager(tmp_path / "repos")
    alias = _legacy_record(3)
    alias["rule"] = "md-MD009"
    alias["finding_id"] = stable_finding_id(REPO, PATH, 3, "md-MD009", DESC)
    _write_jsonl(
        state.get_findings_file("demo"),
        [_legacy_record(3), alias, _legacy_record(4)],
    )

    findings = state.load_findings("demo")

    assert sorted(f.finding_id for f in findings) == sorted(
        [_legacy_id(3), _legacy_id(4)]
    )
    assert {f.rule for f in findings} == {"md-MD009"}
