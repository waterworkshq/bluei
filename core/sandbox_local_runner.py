"""sandbox_local_runner — shim for backward compatibility.

The actual implementation is now in the sandbox_local_runner/ package.
This file redirects imports from the old module location to the new package.
"""
import sys
from pathlib import Path

_this_dir = Path(__file__).parent
if str(_this_dir) not in sys.path:
    sys.path.insert(0, str(_this_dir))

from sandbox_local_runner import *

# main lives in cli submodule. Expose it at the top level.
_mod = sys.modules.get('core.sandbox_local_runner')
if _mod is not None:
    _mod.main = _mod.cli.main

if __name__ == '__main__':
    # Running as a script: delegate to main()
    from sandbox_local_runner.cli import main as _main
    raise SystemExit(_main())
