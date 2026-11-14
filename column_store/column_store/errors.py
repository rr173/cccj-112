"""Exception types shared by the column store.

Every failure path raises one of these; the command line wrapper turns them
into a one-line message on stderr and a non-zero exit status.  No exception
here ever carries a partially built output: callers commit results only after
the whole operation has succeeded.
"""


class ColumnStoreError(Exception):
    """Base class for all deliberate failures."""

    kind = "error"


class SchemaError(ColumnStoreError):
    kind = "schema"


class InputError(ColumnStoreError):
    """A source JSONL record is malformed.  Carries the physical line number."""

    kind = "input"

    def __init__(self, line_no, reason, detail=""):
        self.line_no = line_no
        self.reason = reason
        self.detail = detail
        msg = "input line %d: %s" % (line_no, reason)
        if detail:
            msg += ": " + detail
        super().__init__(msg)


class UsageError(ColumnStoreError):
    kind = "usage"


class StoreFormatError(ColumnStoreError):
    """An on-disk store is not structurally valid."""

    kind = "format"


class StoreIntegrityError(ColumnStoreError):
    """A checksum, bound, or page invariant failed while reading."""

    kind = "integrity"


class OutputRefused(ColumnStoreError):
    """A requested output path already exists (never overwritten)."""

    kind = "output"
