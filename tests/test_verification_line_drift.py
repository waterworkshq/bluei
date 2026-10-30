"""Real-tool regression for diagnostics moving after unrelated autofixes."""

import shutil
import subprocess

import pytest

from bluei.engine.discovery import discover_findings
from bluei.engine.validation import verify_fix_closed


def test_unrelated_import_removal_does_not_close_undefined_name(tmp_path):
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff is not installed")
    (tmp_path / "setup.py").write_text("")
    target = tmp_path / "sample.py"
    target.write_text("import os\n\ndef value():\n    return missing\n")
    log = tmp_path / "run.log"
    docs = tmp_path / "docs-index.json"
    findings = discover_findings(tmp_path, log_file=log, docs_index_file=docs)
    finding = next(f for f in findings if f.rule == "ruff-F821" and f.path == "sample.py")

    result = subprocess.run(
        [ruff, "check", "--isolated", "--no-cache", "--fix", "--select", "F401", "--", "sample.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "import os" not in target.read_text()
    assert not verify_fix_closed(tmp_path, finding, log, docs_index_file=docs)

    target.write_text("def value():\n    return 1\n")
    assert verify_fix_closed(tmp_path, finding, log, docs_index_file=docs)
