"""Markdown legacy bare-rule (MD009) IDs must survive md-MD009 canonicalization.

Truncated snippets (>200 chars share a prefix) must never merge: alias
adoption and read projection require the finding_id to recompute from the
record's own stored fields.
"""

import json

from bluei.app.state import StateManager
from bluei.engine.models import Finding, now_iso
from bluei.engine.state import append_findings, filter_findings_by_cooldown
from plugins.markdown.plugin import stable_finding_id

REPO = "/tmp/example-repo"
PATH = "docs/readme.md"
DESC = "Trailing spaces [Expected: 0 or 2; Actual: 1]"
LONG_A = "tail " * 50 + "AAAA"
LONG_B = "tail " * 50 + "BBBB"  # identical first 200 chars, distinct full text


def _legacy_id(line: int, desc: str = DESC) -> str:
    return stable_finding_id(REPO, PATH, line, "MD009", desc)


def _legacy_record(line: int, desc: str = DESC) -> dict:
    return {
        "finding_id": _legacy_id(line, desc),
        "repo": REPO,
        "path": PATH,
        "line": line,
        "rule": "MD009",
        "snippet": desc[:200],
        "confidence": 0.85,
        "quick_win": False,
        "safe_to_autofix": False,
        "severity": "low",
        "category": "docs",
        "discovered_at": "2026-10-01T00:00:00+00:00",
    }


def _current_finding(line: int, desc: str = DESC) -> Finding:
    rule = "md-MD009"
    return Finding(
        finding_id=stable_finding_id(REPO, PATH, line, rule, desc),
        repo=REPO,
        path=PATH,
        line=line,
        rule=rule,
        snippet=desc[:200],
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


def test_mixed_aliases_adopt_earliest_before_seen(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    canonical = _legacy_record(3)
    canonical["rule"] = "md-MD009"
    canonical["finding_id"] = stable_finding_id(REPO, PATH, 3, "md-MD009", DESC)
    _write_jsonl(findings_file, [_legacy_record(3), canonical])

    finding = _current_finding(3)  # own ID already persisted (in seen)
    assert append_findings(findings_file, [finding]) == 0
    assert finding.finding_id == _legacy_id(3)  # adopted before seen fast-path

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


def test_empty_markdown_ids_do_not_collapse_distinct_records(tmp_path):
    state = StateManager(tmp_path / "repos")
    records = [_legacy_record(line) for line in (3, 4)]
    for record in records:
        record["finding_id"] = ""
    _write_jsonl(state.get_findings_file("demo"), records)
    assert [finding.line for finding in state.load_findings("demo")] == [3, 4]


def test_truncated_snippets_never_merge(tmp_path):
    state = StateManager(tmp_path / "repos")
    findings_file = state.get_findings_file("demo")
    _write_jsonl(findings_file, [_legacy_record(3, LONG_A)])

    finding = _current_finding(3, LONG_B)  # distinct full-hash ID
    own_id = finding.finding_id
    assert append_findings(findings_file, [finding]) == 1
    assert finding.finding_id == own_id  # not adopted; distinct ID preserved

    projected = state.load_findings("demo")
    assert sorted(f.finding_id for f in projected) == sorted(
        [_legacy_id(3, LONG_A), own_id]
    )
    assert {f.rule for f in projected} == {"md-MD009"}
