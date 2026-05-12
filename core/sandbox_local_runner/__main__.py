"""sandbox_local_runner.__main__ — entry point for `python -m sandbox_local_runner`."""
import sys
from sandbox_local_runner.cli import main

try:
    sys.exit(main())
except Exception as e:
    print(f"sandbox_local_runner error: {e}", file=sys.stderr)
    sys.exit(1)
