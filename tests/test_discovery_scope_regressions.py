"""Full discovery must not apply domain-fixture assumptions to arbitrary code."""

import shutil

import pytest

from bluei.engine.discovery import discover_findings


def test_unrelated_application_has_no_sandbox_domain_findings(tmp_path):
    if shutil.which("ruff") is None:
        pytest.skip("ruff is not installed")
    (tmp_path / "setup.py").write_text("")
    (tmp_path / "app.py").write_text(
        "def calculate_total(first, second):\n    return first + second\n\n"
        "if __name__ == '__main__':\n    print(missing_name)\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_orders.py").write_text("def test_total():\n    pass\n")
    outcomes = {}
    findings = discover_findings(tmp_path, log_file=tmp_path / "scan.log",
                                 docs_index_file=tmp_path / "docs.json", outcomes=outcomes)
    forbidden = {"catalog-query-not-normalized", "discount-math-sign", "test-gap-missing-file",
                 "test-gap-missing-case", "notifications-email-no-trim", "notifications-type-guard-missing"}
    assert not [finding for finding in findings if finding.rule in forbidden]
    assert any(finding.rule.lower() == "ruff-f821" and finding.path == "app.py" for finding in findings)
    assert outcomes["python-linter-ruff"]["status"] == "ran"
