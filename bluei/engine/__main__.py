"""bluei.engine.__main__ — entry point for `python -m bluei.engine`."""
import sys
from bluei.engine.cli import main

try:
    sys.exit(main())
except Exception as e:
    print(f"bluei.engine error: {e}", file=sys.stderr)
    sys.exit(1)
