#!/usr/bin/env python
"""Rewrite lake datasets under the partitioning their registry spec declares.

Run this after a partitioning change (or after pulling one) and before any
backfill: reading a dataset that exists in two layouts at once would double
count rows.

    python scripts/migrate_lake_layout.py --dry-run
    python scripts/migrate_lake_layout.py --dataset bars_intraday
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.migrate import _main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_main())
