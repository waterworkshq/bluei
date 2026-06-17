"""Tests for bluei.engine.jsonl primitives."""

import json
from pathlib import Path

import pytest

from bluei.engine.jsonl import read_jsonl, append_jsonl


def test_read_jsonl_missing_file_returns_empty(tmp_path):
    assert read_jsonl(tmp_path / "nonexistent.jsonl") == []


def test_read_jsonl_basic(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\n{"a":2}\n')
    assert read_jsonl(p) == [{"a": 1}, {"a": 2}]


def test_read_jsonl_skips_blank_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\n\n  \n{"a":2}\n')
    assert read_jsonl(p) == [{"a": 1}, {"a": 2}]


def test_read_jsonl_skip_errors_default_true(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\nnot json\n{"a":2}\n')
    assert read_jsonl(p) == [{"a": 1}, {"a": 2}]


def test_read_jsonl_skip_errors_false_raises(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\nnot json\n')
    with pytest.raises(json.JSONDecodeError):
        read_jsonl(p, skip_errors=False)


def test_read_jsonl_limit_returns_last_n(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\n{"a":2}\n{"a":3}\n')
    assert read_jsonl(p, limit=2) == [{"a": 2}, {"a": 3}]


def test_read_jsonl_dicts_only_filters_non_dicts(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"a":1}\n[1,2]\n"string"\n42\n{"b":2}\n')
    assert read_jsonl(p, dicts_only=True) == [{"a": 1}, {"b": 2}]


def test_append_jsonl_creates_parents(tmp_path):
    p = tmp_path / "subdir" / "f.jsonl"
    append_jsonl(p, {"x": 1})
    assert p.read_text() == '{"x": 1}\n'


def test_append_jsonl_appends_in_order(tmp_path):
    p = tmp_path / "f.jsonl"
    append_jsonl(p, {"a": 1})
    append_jsonl(p, {"a": 2})
    assert read_jsonl(p) == [{"a": 1}, {"a": 2}]


def test_append_jsonl_sort_keys_default_true(tmp_path):
    p = tmp_path / "f.jsonl"
    append_jsonl(p, {"b": 1, "a": 2})
    assert p.read_text() == '{"a": 2, "b": 1}\n'


def test_append_jsonl_round_trip(tmp_path):
    p = tmp_path / "f.jsonl"
    records = [{"i": i, "s": str(i)} for i in range(10)]
    for r in records:
        append_jsonl(p, r)
    assert read_jsonl(p) == records
