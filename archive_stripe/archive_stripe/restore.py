"""Restore a source file from a stripe pack.

The restore path is deliberately paranoid:

1. Read and fully validate the manifest.  Missing / contradictory manifests
   stop here; the layout is never guessed.
2. Hash every stripe and classify it (see :mod:`check`).  Anything that is
   missing, truncated, oversized, rewritten, or unreadable is treated as a
   *whole-stripe erasure* — damaged bytes never enter the parity equations.
3. If more than two stripes are untrustworthy, refuse before writing.
4. Reconstruct into a temporary file, truncate to the manifest's true source
   length, verify the source SHA-256, and only then rename over the caller's
   target path.  A failed restore never touches an existing target.
"""

import os

from .check import check_pack, inspect_stripe
from .codec import restore_to_file
from .errors import (
    InsufficientStripeError,
    LayoutError,
    ManifestError,
    RestoreRefused,
)
from .manifest import read_manifest


class RestoreResult:
    __slots__ = ("path", "bytes_written", "source_sha256", "used_stripes",
                 "reconstructed_stripes", "discarded_stripes", "report")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw[slot])


def restore_pack(pack_dir, out_path, *, progress=None):
    """Restore ``pack_dir`` into *out_path* (a new file written by us)."""
    if not os.path.isdir(pack_dir):
        raise ManifestError(f"pack directory does not exist: {pack_dir}")
    manifest = read_manifest(pack_dir)

    rows = [inspect_stripe(pack_dir, e) for e in manifest.stripes]
    trusted = sorted(r["name"] for r in rows if r["trustworthy"])
    discarded = [r["name"] for r in rows if not r["trustworthy"]]
    if len(trusted) < manifest.k:
        raise InsufficientStripeError(
            f"only {len(trusted)} trustworthy stripes remain "
            f"({', '.join(trusted) or 'none'}); k={manifest.k} are required. "
            "Refusing to guess."
        )
    data_lost = [n for n in discarded if n.startswith("D")]
    if len(data_lost) > 2:
        raise InsufficientStripeError(
            f"{len(data_lost)} data stripes are untrustworthy; double "
            "redundancy can rebuild at most two"
        )
    if out_path is None:
        raise RestoreRefused("no output path given")
    out_path = os.path.abspath(out_path)
    if os.path.isdir(out_path):
        raise RestoreRefused(f"output path is a directory: {out_path}")
    if os.path.exists(out_path):
        raise RestoreRefused(
            f"output file already exists: {out_path} (restore writes a new "
            "file and never overwrites)"
        )
    out_dir = os.path.dirname(out_path)
    if not os.path.isdir(out_dir):
        raise RestoreRefused(f"output directory does not exist: {out_dir}")
    if not os.access(out_dir, os.W_OK):
        raise RestoreRefused(f"output directory is not writable: {out_dir}")

    audit = check_pack(pack_dir)  # same rows, plus recovery mode / reason
    try:
        written = restore_to_file(
            pack_dir, manifest, out_path, set(trusted), progress=progress
        )
    except InsufficientStripeError:
        raise
    except LayoutError as exc:
        raise RestoreRefused(f"restore refused: {exc}") from exc

    return RestoreResult(
        path=out_path,
        bytes_written=written,
        source_sha256=manifest.source_sha256,
        used_stripes=trusted,
        reconstructed_stripes=[n for n in manifest.all_names() if n not in trusted],
        discarded_stripes=discarded,
        report=audit,
    )
