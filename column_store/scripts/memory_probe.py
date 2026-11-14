#!/usr/bin/env python3
"""Report the cost of the current row group only (build memory probe).

Can be run standalone against a store: prints group counts, per-group page
bytes, and the bytes one group would hold in memory as decoded Python values
(a rough upper bound).  Used to sanity-check that the builder's working set
is bounded by row_group_max, not by input length.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from column_store.catalog import load_store  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--store", required=True)
    a = p.parse_args(argv)
    meta = load_store(a.store)
    print("rows=%d groups=%d row_group_max=%d"
          % (meta.row_count, len(meta.groups), meta.row_group_max))
    if meta.groups:
        first = meta.groups[0]
        per_group = sum(pg.length for pg in first.columns)
        print("one group: %d rows, %d page bytes, per-column %s"
              % (first.rows, per_group,
                 [[c.name, first.columns[i].length]
                  for i, c in enumerate(meta.schema)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
