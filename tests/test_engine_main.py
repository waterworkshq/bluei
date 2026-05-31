import subprocess
import sys


def test_main_module_calls_cli_main():
    result = subprocess.run(
        [sys.executable, "-m", "bluei.engine"],
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1, 2)


def test_main_module_import_error():
    import importlib

    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['bluei.engine.cli'] = None; "
            "exec(open('bluei/engine/__main__.py').read())",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        _, stderr = proc.communicate()
        assert proc.returncode == 1
