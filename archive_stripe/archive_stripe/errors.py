"""Error types for archive stripe packs.

Every failure is a subclass of :class:`ArchiveError`; the CLI maps it to
exit code 2.  Messages name the file and, where relevant, the stripe so the
source archive stays diagnosable.
"""


class ArchiveError(Exception):
    """Base class for all archive failures."""


class ManifestError(ArchiveError):
    """The manifest is missing, unreadable, malformed, or self-contradictory."""


class LayoutError(ArchiveError):
    """Parameters are outside the format bounds or do not describe a valid pack."""


class StripeError(ArchiveError):
    """A stripe file is missing, truncated, oversized, or rewritten."""


class InsufficientStripeError(ArchiveError):
    """Too few trustworthy stripes remain to reconstruct the file."""


class RestoreRefused(ArchiveError):
    """Restore refuses to write output; the existing target is untouched."""
