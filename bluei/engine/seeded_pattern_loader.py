"""Load product-fixture seeded patterns into FixPatternStore's in-memory index.

Mirrors bundle_loader.load_bundles() pattern (ADR-0009 hybrid model).
Seeded patterns are read-only product fixtures — loaded into the index
but never written to the repo-local JSONL file.

Called by open_pattern_store() after construction. Product patterns take
precedence on structural-hash conflict with repo-local patterns.
"""

import logging
from pathlib import Path
from typing import Optional

from bluei.engine.jsonl import read_jsonl
from bluei.engine.pattern_store import FixPattern, FixPatternStore

_logger = logging.getLogger(__name__)

_PRODUCT_SEEDED_DIR = Path(__file__).parent / "seeded_patterns"


def load_seeded_patterns(
    store: FixPatternStore,
    seeded_dir: Optional[Path] = None,
) -> int:
    """Load product-fixture seeded patterns into the store's in-memory index.

    Reads all *.jsonl files from seeded_dir (default: bluei/engine/seeded_patterns/).
    Each record is a FixPattern. Patterns are inserted via store._index_pattern()
    WITHOUT writing to disk.

    On structural-hash conflict with a repo-local (mined) pattern, the seeded
    pattern takes precedence (product wins, same convention as bundles).

    Returns the number of seeded patterns loaded.
    """
    directory = seeded_dir or _PRODUCT_SEEDED_DIR
    if not directory.is_dir():
        return 0

    count = 0
    for jsonl_path in sorted(directory.glob("*.jsonl")):
        for record in read_jsonl(jsonl_path):
            if not isinstance(record, dict):
                continue
            try:
                pattern = FixPattern.from_dict(record)
            except Exception as exc:
                _logger.debug(
                    "Skipping malformed seeded pattern in %s: %s", jsonl_path, exc
                )
                continue

            # Product wins on structural-hash conflict with repo-local patterns.
            # If a mined pattern with the same structural hash exists, replace it.
            s_hash = pattern.structural_hash
            if s_hash:
                existing_pid = store._structural_index.get(pattern.rule, {}).get(s_hash)
                if existing_pid and existing_pid in store._patterns:
                    existing = store._patterns[existing_pid]
                    if existing.source != "authoritative-seed":
                        del store._patterns[existing_pid]

            store._index_pattern(pattern)
            count += 1
    return count
