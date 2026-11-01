"""Direct same-instance lock re-entry: diagnostics, never an automatic repair."""
import textwrap

import pytest

from bluei.engine.ast_engine import get_python_matcher
from bluei.engine.commands.pr_cycle import _model_lane_eligible
from bluei.engine.discovery import discover_findings
from bluei.engine.validation import responsible_detector_ran

RULE = "lock-self-reentry"
SOURCE = textwrap.dedent('''\
    import threading

    class Monitor:
        def __init__(self):
            self.guard = threading.Lock()

        def dashboard(self):
            with self.guard:
                return self.snapshot()

        def snapshot(self):
            with self.guard:
                return 1
''')


def matches(source):
    return [match for match in get_python_matcher().find_matches(source, "ordinary.py")
            if match.pattern.id == RULE]


@pytest.mark.parametrize("statement", ["return self.snapshot()", "value = self.snapshot()", "self.snapshot()"])
def test_reports_held_call_not_lock_allocation(statement):
    found = matches(SOURCE.replace("return self.snapshot()", statement))
    assert len(found) == 1
    assert found[0].line == 9 and found[0].source_text == "self.snapshot()"


def test_plain_lock_import_and_distinct_names():
    source = SOURCE.replace("import threading", "from threading import Lock").replace(
        "threading.Lock()", "Lock()"
    ).replace("guard", "mutex").replace("dashboard", "outer").replace("snapshot", "inner")
    assert len(matches(source)) == 1


def test_direct_annotated_lock_initialization():
    assert len(matches(SOURCE.replace("self.guard =", "self.guard: object ="))) == 1


def test_immediate_acquire_without_arguments():
    source = SOURCE.replace("        with self.guard:\n            return 1", "        self.guard.acquire()\n        return 1")
    assert len(matches(source)) == 1


def test_separate_classes_and_duplicate_calls_are_not_generic_call_matches():
    source = SOURCE + "\n" + SOURCE.replace("import threading\n", "").replace("Monitor", "Other")
    assert len(matches(source)) == 2
    assert matches("class A:\n    def f(self):\n        return self.g()\n") == []


@pytest.mark.parametrize("source", [
    SOURCE.replace("threading.Lock()", "threading.RLock()"),
    SOURCE.replace("import threading", "import threading as t").replace("threading.Lock", "t.Lock"),
    SOURCE.replace("import threading", "from threading import Lock as L").replace("threading.Lock", "L"),
    SOURCE.replace("import threading", ""),
    SOURCE.replace("import threading", "import threading\nthreading = unknown"),
    SOURCE.replace("import threading", "import threading\nthreading.Lock = unknown"),
    SOURCE.replace("class Monitor:", "class Monitor(Base):"),
    SOURCE.replace("class Monitor:", "@decorate\nclass Monitor:"),
    SOURCE.replace("    def snapshot(self):", "    @decorate\n    def snapshot(self):"),
    SOURCE.replace("    def dashboard(self):", "    @decorate\n    def dashboard(self):"),
    SOURCE.replace("    def __init__(self):", "    @decorate\n    def __init__(self):"),
    SOURCE.replace("    def snapshot(self):", "    async def snapshot(self):"),
    SOURCE.replace("    def dashboard(self):", "    async def dashboard(self):"),
    SOURCE.replace("threading.Lock()", "factory()"),
    SOURCE.replace("            return 1", "            yield 1"),
    SOURCE.replace("        with self.guard:\n            return 1", "        return 1\n        with self.guard:\n            return 2"),
    SOURCE.replace("        with self.guard:\n            return 1", "        if enabled:\n            with self.guard:\n                return 1"),
    SOURCE.replace("        with self.guard:\n            return 1", "        with self.other:\n            return 1"),
    SOURCE.replace("            return self.snapshot()", "            return other.snapshot()"),
    SOURCE.replace("        with self.guard:\n            return self.snapshot()", "        return self.snapshot()"),
    SOURCE.replace("            return self.snapshot()", "            def later():\n                return self.snapshot()"),
    SOURCE.replace("            return self.snapshot()", "            return lambda: self.snapshot()"),
    SOURCE.replace("            return self.snapshot()", "            return (self.snapshot() for _ in items)"),
    SOURCE.replace("            return self.snapshot()", "            return\n            self.snapshot()"),
    SOURCE.replace("            return self.snapshot()", "            self.guard.release()\n            return self.snapshot()"),
    SOURCE.replace("            return self.snapshot()", "            self.mutate()\n            return self.snapshot()"),
    SOURCE.replace("            return self.snapshot()", "            if False:\n                return self.snapshot()"),
    SOURCE.replace("self.guard = threading.Lock()", "self.guard = threading.Lock()\n        self.guard = threading.RLock()"),
    SOURCE + "\n    def reset(self):\n        self.guard = threading.RLock()\n",
    SOURCE + "\n    def reset(self):\n        self.snapshot = replacement\n",
    SOURCE + "\n    snapshot = replacement\n",
    SOURCE + "\nMonitor.snapshot = replacement\n",
    SOURCE + "\nsetattr(Monitor, 'snapshot', replacement)\n",
    SOURCE + "\nimport builtins\nbuiltins.setattr(Monitor, 'snapshot', replacement)\n",
    SOURCE + "\nchange = setattr\nchange(Monitor, 'snapshot', replacement)\n",
    SOURCE + "\nvars(Monitor)['snapshot'] = replacement\n",
    SOURCE + "\nMonitor.__init__.__defaults__ = (replacement,)\n",
    SOURCE + "\nMonitor.snapshot.__code__ = replacement.__code__\n",
    SOURCE + "\nMonitor.__dict__['snapshot'] = replacement\n",
    SOURCE + "\nitem = Monitor()\nitem.guard = threading.RLock()\n",
    SOURCE.replace("    def snapshot(self):", "    guard = descriptor()\n\n    def snapshot(self):"),
    SOURCE.replace("    def dashboard(self):", "    def reset(self):\n        object.__setattr__(self, 'snapshot', replacement)\n\n    def dashboard(self):"),
    SOURCE.replace("        with self.guard:\n            return self.snapshot()", "        self.guard.acquire()\n        return self.snapshot()"),
    SOURCE.replace("self.guard = threading.Lock()", "return\n        self.guard = threading.Lock()"),
    SOURCE.replace("self.guard = threading.Lock()", "def later():\n            self.guard = threading.Lock()"),
    SOURCE.replace("self.guard = threading.Lock()", "if enabled:\n            self.guard = threading.Lock()"),
    SOURCE.replace("    def dashboard(self):", "    def dashboard(self):\n        self = other"),
    SOURCE.replace("        with self.guard:\n            return 1", "        self.guard.acquire(False)\n        return 1"),
    SOURCE.replace("    def snapshot(self):", "    def snapshot(other):"),
    SOURCE.replace("    def snapshot(self):", "    def snapshot(self, required):"),
    SOURCE.replace("    def snapshot(self):", "    def snapshot(self, *, required):"),
    SOURCE.replace("self.snapshot()", "self.snapshot(release_guard())"),
    SOURCE + "\nclass Child(Monitor):\n    pass\n",
])
def test_ambiguous_or_deferred_shapes_do_not_report(source):
    import ast
    assert source != SOURCE
    ast.parse(source)
    assert matches(source) == []


@pytest.mark.parametrize("scope", ["repository", "python"])
def test_shipped_discovery_has_producer_evidence_but_no_repair_route(tmp_path, scope):
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "monitor.py").write_text(SOURCE)
    outcomes = {}
    found = [finding for finding in discover_findings(
        tmp_path, log_file=tmp_path / "scan.log", outcomes=outcomes, discovery_scope=scope
    ) if finding.rule == RULE]
    assert len(found) == 1
    finding = found[0]
    assert (finding.path, finding.line, finding.confidence) == ("monitor.py", 9, 0.9)
    assert not finding.quick_win and not finding.safe_to_autofix
    assert outcomes["ast-python"]["status"] == "ran"
    assert responsible_detector_ran(RULE, outcomes)
    assert not _model_lane_eligible(finding)


def test_module_walk_count_does_not_grow_with_classes(monkeypatch):
    import ast
    import bluei.engine.ast_engine.lock_reentry as detector
    source = "import threading\n" + "\n".join(
        SOURCE.replace("import threading\n", "").replace("Monitor", f"Monitor{index}")
        for index in range(30)
    )
    tree = ast.parse(source)
    original_walk = ast.walk
    module_walks = []
    def counted_walk(node):
        if node is tree:
            module_walks.append(True)
        return original_walk(node)
    monkeypatch.setattr(detector.ast, "walk", counted_walk)
    assert len(detector.find_lock_reentries(tree)) == 30
    assert len(module_walks) == 1


def test_deep_attribute_constructor_does_not_escape_matcher():
    source = SOURCE.replace("threading.Lock()", "other." + "deep." * 1200 + "Lock()")
    assert matches(source) == []


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_finder_resource_failure_is_incomplete_not_clean(tmp_path, monkeypatch, error):
    import bluei.engine.ast_engine.matcher as matcher_module
    def fail(tree):
        raise error("bounded test")
    monkeypatch.setattr(matcher_module, "find_lock_reentries", fail)
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "monitor.py").write_text(SOURCE)
    outcomes = {}
    discover_findings(tmp_path, log_file=tmp_path / "scan.log", outcomes=outcomes,
                      discovery_scope="python")
    assert outcomes["ast-python"]["status"] == "failed"


def test_only_the_actual_ast_producer_can_prove_rule_completion():
    assert responsible_detector_ran(RULE, {"ast-python": {"status": "ran"}})
    assert not responsible_detector_ran(RULE, {"ast-python": {"status": "failed"}})
    assert not responsible_detector_ran(RULE, {"plugin:plugin-python": {"status": "ran"}})
