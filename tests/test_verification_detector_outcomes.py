"""A detector that did not run cannot prove a finding is gone."""

import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from bluei.engine.models import Finding
from bluei.engine.validation import build_target_checks, verify_fix_closed


def test_real_missing_ruff_does_not_close_its_finding(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("print(missing_name)\n")
    (tmp_path / "setup.py").write_text("")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    finding = Finding("missing", str(tmp_path), "app.py", 1, "ruff-F821",
                      "Undefined name `missing_name`", 0.9, False, False)
    log = tmp_path / "verification.log"
    assert not verify_fix_closed(tmp_path, finding, log, docs_index_file=tmp_path / "docs.json")
    assert "incomplete discovery" in log.read_text()


def test_actual_real_ruff_rescan_can_close_resolved_finding(tmp_path):
    if shutil.which("ruff") is None:
        pytest.skip("ruff is not installed")
    (tmp_path / "app.py").write_text("print('resolved')\n")
    (tmp_path / "setup.py").write_text("")
    finding = Finding("old-missing", str(tmp_path), "app.py", 1, "ruff-F821",
                      "Undefined name `missing_name`", 0.9, False, False)
    assert verify_fix_closed(tmp_path, finding, tmp_path / "verification.log",
                             docs_index_file=tmp_path / "docs.json")


def test_validation_path_is_data_not_python_source(tmp_path):
    filename = 'guide\"); __import__("pathlib").Path("injected").touch(); #.md'
    (tmp_path / filename).write_text("Current documentation\n")
    finding = SimpleNamespace(rule="docs-legacy-reference", path=filename)
    command = build_target_checks(finding)["target_docs_legacy_reference"]
    assert command[-1] == filename
    command[0] = sys.executable
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "injected").exists()
