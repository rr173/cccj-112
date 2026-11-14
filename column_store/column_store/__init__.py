"""A small column-oriented event store built from UTF-8 JSONL.

Public entry points live in :mod:`column_store.cli`:

* ``build`` imports a UTF-8 JSONL file into a new store directory;
* ``scan`` filters by ANDed predicates and projects columns to JSONL.

The on-disk format is documented in ``docs/FORMAT.md``.
"""

__version__ = "1.0"
