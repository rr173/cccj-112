"""Error types for the external left-join job."""


class JoinError(Exception):
    """Base class for every join failure reported with side and row number."""


class KeyError_(JoinError):
    """A key field is missing, null, or has a value of the wrong type.

    Missing / null keys are *not* errors by themselves (they simply do not
    match); this exception is raised only for a present, non-null value whose
    JSON type does not match the configured component type.
    """

    def __init__(self, side, row, field, expected, actual):
        self.side = side
        self.row = row
        self.field = field
        self.expected = expected
        self.actual = actual
        kind = actual.__class__.__name__
        super().__init__(
            f"{side} side row {row}: key field {field!r} expects {expected}, "
            f"got non-null value of type {kind}"
        )


class InputDecodeError(JoinError):
    """A line of an input file is not a single valid JSON object."""

    def __init__(self, side, row, detail):
        self.side = side
        self.row = row
        super().__init__(f"{side} side row {row}: invalid JSON object ({detail})")
