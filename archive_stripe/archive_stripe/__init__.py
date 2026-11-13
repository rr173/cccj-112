"""Two-fault-tolerant archival stripes for raw capture bytes.

``pack``     encode any binary file into k data + P + Q stripe files.
``check``    classify every stripe (ok / missing / truncated / rewritten ...).
``restore``  rebuild the original file from any k trustworthy stripes.
"""

from .check import check_pack, pack_source
from .errors import (
    ArchiveError,
    InsufficientStripeError,
    LayoutError,
    ManifestError,
    RestoreRefused,
    StripeError,
)
from .manifest import FORMAT_VERSION, read_manifest
from .restore import restore_pack

__all__ = [
    "pack_source",
    "check_pack",
    "restore_pack",
    "read_manifest",
    "FORMAT_VERSION",
    "ArchiveError",
    "ManifestError",
    "LayoutError",
    "StripeError",
    "InsufficientStripeError",
    "RestoreRefused",
]
