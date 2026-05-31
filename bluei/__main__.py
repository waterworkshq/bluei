"""Allow ``python -m bluei`` and provide a setuptools entry point."""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from bin.bluei import main  # noqa: E402


def run():
    sys.exit(main())


if __name__ == "__main__":
    run()
