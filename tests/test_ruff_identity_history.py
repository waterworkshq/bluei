"""Ruff case-variant rule ids (ruff-B904 vs ruff-b904) must share history.

Identity collapses on the canonical lowercase rule so re-discovered
diagnostics adopt the earliest persisted finding_id. Truncated snippets
(>200 chars share a prefix) never merge: adoption requires the
finding_id to recompute from the record's own stored fields. Snippets
compare verbatim — a trailing-whitespace difference (E501 messages embed
source line text) is ambiguous and must not merge records.
"""

import json

from bluei.engine.models import Finding, now_iso, stable_finding_id
from bluei.engine.state import _load_ruff_identities, append_findings

REPO = "/tmp/example-repo"
PATH = "src/app.py"
MSG = "Within an `except` clause, use `raise ... from err` to preserve traceback"
LONG_A = "tail " * 50 + "AAAA"
LONG_B = "tail " * 50 + "BBBB"  # identical first 200 chars, distinct full text


def _legacy_id(line: int, rule: str = "ruff-B904", msg: str = MSG) -> str:
    return stable_finding_id(REPO, PATH, line, rule, msg)


def _legacy_record(line: int, rule: str = "ruff-B904", msg: str = MSG) -> dict:
    return {
        "finding_id": _legacy_id(line, rule, msg),
        "repo": REPO,
        "path": PATH,
        "line": line,
        "rule": rule,
        "snippet": msg[:200],
        "confidence": 0.88,
        "quick_win": True,
        "safe_to_autofix": True,
        "severity": "medium",
        "category": "",
        "discovered_at": "2026-10-01T00:00:00+00:00",
    }


def _current_finding(line: int, msg: str = MSG, snippet: str | None = None) -> Finding:
    rule = "ruff-b904"
    return Finding(
        finding_id=stable_finding_id(REPO, PATH, line, rule, msg),
        repo=REPO,
        path=PATH,
        line=line,
        rule=rule,
        snippet=(msg[:200] if snippet is None else snippet),
        confidence=0.88,
        quick_win=True,
        safe_to_autofix=True,
        severity="medium",
        category="",
        discovered_at=now_iso(),
    )


def _write_jsonl(path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in records),
        encoding="utf-8",
    )


def test_append_adopts_legacy_case_variant_id_and_preserves_jsonl(tmp_path):
    findings_file = tmp_path / "repos" / "demo" / "state" / "findings.jsonl"
    _write_jsonl(findings_file, [_legacy_record(3)])
    before = findings_file.read_text(encoding="utf-8")

    f3, f4 = _current_finding(3), _current_finding(4)
    assert append_findings(findings_file, [f3, f4]) == 1  # 3 adopted, 4 new
    assert f3.finding_id == _legacy_id(3)
    lines = findings_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0] == before.splitlines()[0]


def test_mixed_case_history_adopts_earliest_before_seen(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    canonical = _legacy_record(3)
    canonical["rule"] = "ruff-b904"
    canonical["finding_id"] = _legacy_id(3, "ruff-b904")
    _write_jsonl(findings_file, [_legacy_record(3), canonical])

    finding = _current_finding(3)  # own ID already persisted (in seen)
    assert append_findings(findings_file, [finding]) == 0
    assert finding.finding_id == _legacy_id(3)  # adopted before seen fast-path


def test_truncated_snippets_never_merge(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    _write_jsonl(findings_file, [_legacy_record(3, msg=LONG_A)])

    finding = _current_finding(3, msg=LONG_B)  # distinct full-hash ID
    own_id = finding.finding_id
    assert append_findings(findings_file, [finding]) == 1
    assert finding.finding_id == own_id  # not adopted; distinct ID preserved


def test_malformed_and_unhashable_records_skipped(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    findings_file.parent.mkdir(parents=True, exist_ok=True)
    bad_record = _legacy_record(3)
    bad_record["line"] = [3]  # unhashable identity component
    findings_file.write_text(
        json.dumps(_legacy_record(3))
        + "\nnot json\n"
        + json.dumps([1, 2])
        + "\n"
        + json.dumps(bad_record)
        + "\n"
        + json.dumps({**_legacy_record(4), "finding_id": ""})
        + "\n"
        + json.dumps({**_legacy_record(5), "rule": "bluei-internal"})
        + "\n"
        + json.dumps({**_legacy_record(6), "rule": "ruff-x1"})
        + "\n"
        + json.dumps({**_legacy_record(7), "rule": "md-MD009"})
        + "\n",
        encoding="utf-8",
    )

    identities = _load_ruff_identities(findings_file)
    assert set(identities.values()) == {_legacy_id(3)}  # only the valid record

    finding = _current_finding(4)
    assert append_findings(findings_file, [finding]) == 1
    assert finding.finding_id == _legacy_id(4, "ruff-b904")


def test_aliases_group_all_historical_ids(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    canonical = _legacy_record(3)
    canonical["rule"] = "ruff-b904"
    canonical["finding_id"] = _legacy_id(3, "ruff-b904")
    _write_jsonl(findings_file, [_legacy_record(3), canonical])

    aliases: dict[str, set[str]] = {}
    identities = _load_ruff_identities(findings_file, aliases=aliases)
    early = _legacy_id(3)
    late = _legacy_id(3, "ruff-b904")
    assert identities[list(identities)[0]] == early  # earliest wins
    assert aliases[early] == {early, late}  # every historical id in one group
    assert aliases[late] is aliases[early]  # same group object


def test_intra_batch_duplicate_appended_once(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    first, dup = _current_finding(3), _current_finding(3)
    assert append_findings(findings_file, [first, dup]) == 1
    assert len(findings_file.read_text(encoding="utf-8").splitlines()) == 1
    # and a later batch with the now-persisted id still writes nothing
    assert append_findings(findings_file, [_current_finding(3)]) == 0


def test_trailing_whitespace_snippet_difference_does_not_merge(tmp_path):
    findings_file = tmp_path / "findings.jsonl"
    _write_jsonl(findings_file, [_legacy_record(3)])  # snippet without tail ws

    # Same case-variant identity but snippet carries trailing whitespace:
    # ambiguous (E501 embeds source text) — must NOT adopt the legacy id.
    finding = _current_finding(3, snippet=MSG[:200] + " ")
    own_id = finding.finding_id
    assert append_findings(findings_file, [finding]) == 1
    assert finding.finding_id == own_id
    assert len(findings_file.read_text(encoding="utf-8").splitlines()) == 2
