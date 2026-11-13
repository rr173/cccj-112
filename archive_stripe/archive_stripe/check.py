"""Health checking and pack installation for archive stripe packs.

``check_pack`` compares every manifest entry with the bytes actually in the
pack directory.  It distinguishes:

* ``ok``              — present, exact expected length, exact SHA-256
* ``missing``         — no such file
* ``truncated``       — shorter than the manifest says
* ``length_mismatch`` — longer than the manifest says (also untrusted)
* ``rewritten``       — exact length but different SHA-256
* ``unreadable``      — exists but cannot be opened as a regular file

Only ``ok`` stripes are ever marked trustworthy.  ``check`` never repairs
anything and never silently accepts a damaged stripe.
"""

import hashlib
import os

from .codec import plan_recovery
from .errors import ArchiveError, InsufficientStripeError, LayoutError, ManifestError
from .manifest import (
    MANIFEST_NAME,
    Manifest,
    read_manifest,
    stripe_names,
    write_manifest,
)

_HASH_BUF = 1 << 20

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_TRUNCATED = "truncated"
STATUS_LENGTH_MISMATCH = "length_mismatch"
STATUS_REWRITTEN = "rewritten"
STATUS_UNREADABLE = "unreadable"
STATUS_NOT_REGULAR = "not_regular"


def hash_file(path):
    """Return ``(size, sha256hex, error)`` without buffering the whole file."""
    sha = hashlib.sha256()
    size = 0
    try:
        if not os.path.exists(path):
            return 0, None, STATUS_MISSING
        if not os.path.isfile(path):
            return 0, None, STATUS_NOT_REGULAR
        with open(path, "rb", buffering=_HASH_BUF) as fh:
            while True:
                chunk = fh.read(_HASH_BUF)
                if not chunk:
                    break
                sha.update(chunk)
                size += len(chunk)
    except FileNotFoundError:
        return 0, None, STATUS_MISSING
    except OSError:
        return 0, None, STATUS_UNREADABLE
    return size, sha.hexdigest(), None


def inspect_stripe(pack_dir, entry):
    """Classify one stripe against its manifest entry."""
    path = os.path.join(pack_dir, entry["name"])
    actual_length, actual_sha, error = hash_file(path)
    row = {
        "name": entry["name"],
        "role": entry["role"],
        "index": entry["index"],
        "expected_length": entry["length"],
        "expected_sha256": entry["sha256"],
        "actual_length": None if error == STATUS_MISSING else actual_length,
        "actual_sha256": actual_sha,
        "status": STATUS_OK,
        "trustworthy": False,
    }
    if error in (STATUS_MISSING, STATUS_UNREADABLE, STATUS_NOT_REGULAR):
        row["status"] = error
        return row
    if actual_length < entry["length"]:
        row["status"] = STATUS_TRUNCATED
        return row
    if actual_length > entry["length"]:
        row["status"] = STATUS_LENGTH_MISMATCH
        return row
    if actual_sha != entry["sha256"]:
        row["status"] = STATUS_REWRITTEN
        return row
    row["trustworthy"] = True
    return row


def check_pack(pack_dir):
    """Return a report dict describing the actual health of a pack.

    The manifest is validated first.  If it is missing or self-contradictory
    the report says so and no stripe is marked trustworthy: without the
    manifest there is no defensible layout to check against.
    """
    report = {
        "pack_dir": os.path.abspath(pack_dir),
        "manifest": {"status": "ok", "error": None},
        "k": None,
        "block_size": None,
        "source_length": None,
        "source_sha256": None,
        "stripe_length": None,
        "stripes": [],
        "healthy": False,
        "recoverable": False,
        "missing_data": [],
        "trusted": [],
        "lost": [],
        "extra_files": [],
        "reason": None,
    }
    if not os.path.isdir(pack_dir):
        report["manifest"] = {
            "status": "missing",
            "error": f"pack directory does not exist: {pack_dir}",
        }
        report["reason"] = "pack directory does not exist"
        return report
    try:
        manifest = read_manifest(pack_dir)
    except (ManifestError, LayoutError) as exc:
        report["manifest"] = {"status": "invalid", "error": str(exc)}
        report["reason"] = str(exc)
        return report

    report["manifest"] = {"status": "ok", "error": None}
    report["k"] = manifest.k
    report["block_size"] = manifest.block_size
    report["source_length"] = manifest.source_length
    report["source_sha256"] = manifest.source_sha256
    report["stripe_length"] = manifest.stripe_length
    rows = [inspect_stripe(pack_dir, entry) for entry in manifest.stripes]
    report["stripes"] = rows
    trusted = [r["name"] for r in rows if r["trustworthy"]]
    report["trusted"] = sorted(trusted)
    report["lost"] = [r["name"] for r in rows if not r["trustworthy"]]
    report["healthy"] = len(trusted) == manifest.k + 2
    known = set(manifest.all_names()) | {MANIFEST_NAME}
    try:
        report["extra_files"] = sorted(
            n for n in os.listdir(pack_dir) if n not in known
        )
    except OSError:
        report["extra_files"] = []
    try:
        missing, mode = plan_recovery(manifest.k, set(trusted))
        report["missing_data"] = [f"D{i}.bin" for i in missing]
        report["recovery_mode"] = mode
        report["recoverable"] = True
    except InsufficientStripeError as exc:
        report["recoverable"] = False
        report["reason"] = str(exc)
    except ArchiveError as exc:  # pragma: no cover - defensive
        report["recoverable"] = False
        report["reason"] = str(exc)
    return report


# ---------------------------------------------------------------------------
# Pack installation
# ---------------------------------------------------------------------------


def _install_staged(staging_dir, pack_dir, force):
    """Move finished stripe files (and manifest) from staging into place."""
    if os.path.exists(pack_dir) and not os.path.isdir(pack_dir):
        raise LayoutError(f"pack target exists and is not a directory: {pack_dir}")
    if not os.path.exists(pack_dir):
        os.replace(staging_dir, pack_dir)
        return
    existing = os.listdir(pack_dir)
    if existing:
        if not force:
            raise LayoutError(
                f"pack directory is not empty: {pack_dir} (use --force to "
                "replace an existing pack)"
            )
        # Remove only files this format owns; leave anything else and fail
        # rather than deleting unknown user data.
        owned = set(stripe_names(8)) | {MANIFEST_NAME}
        unknown = [n for n in existing if n not in owned]
        if unknown:
            raise LayoutError(
                f"pack directory contains files not owned by a pack: "
                f"{sorted(unknown)[:5]}"
            )
        # Drop the manifest first so a half-installed pack is never trusted.
        try:
            os.unlink(os.path.join(pack_dir, MANIFEST_NAME))
        except FileNotFoundError:
            pass
        for name in sorted(owned):
            if name == MANIFEST_NAME:
                continue
            try:
                os.unlink(os.path.join(pack_dir, name))
            except FileNotFoundError:
                pass
    for name in sorted(os.listdir(staging_dir)):
        os.replace(os.path.join(staging_dir, name), os.path.join(pack_dir, name))
    os.rmdir(staging_dir)


def pack_source(source_path, pack_dir, k, block_size, force=False):
    """Create a complete pack atomically at *pack_dir*.

    Stripe files and manifest are produced in a staging directory next to
    the target; the manifest is written only after all stripe files are
    durable.  A failure leaves any previous pack untouched.
    """
    from .codec import encode_file  # local import keeps module cycles simple

    if not os.path.isfile(source_path):
        raise LayoutError(f"source file does not exist: {source_path}")
    pack_dir = os.path.abspath(pack_dir)
    parent = os.path.dirname(pack_dir)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(pack_dir) and not os.path.isdir(pack_dir):
        raise LayoutError(f"pack target exists and is not a directory: {pack_dir}")
    if os.path.isdir(pack_dir) and os.listdir(pack_dir) and not force:
        raise LayoutError(
            f"pack directory is not empty: {pack_dir} (use --force to replace "
            "an existing pack)"
        )
    staging = os.path.join(
        parent, f".{os.path.basename(pack_dir)}.staging-{os.getpid()}"
    )
    if os.path.exists(staging):
        import shutil

        shutil.rmtree(staging)
    os.mkdir(staging)
    try:
        result = encode_file(source_path, staging, k, block_size)
        entries = [
            {
                "name": e["name"],
                "role": e["role"],
                "index": e["index"],
                "length": e["length"],
                "sha256": e["sha256"],
            }
            for e in result.stripes
        ]
        manifest = Manifest(
            version=1,
            k=result.k,
            block_size=result.block_size,
            source_length=result.source_length,
            source_sha256=result.source_sha256,
            stripe_length=result.stripe_length,
            stripes=entries,
        )
        write_manifest(staging, manifest)
        _install_staged(staging, pack_dir, force)
    except BaseException:
        import shutil

        try:
            shutil.rmtree(staging)
        except OSError:
            pass
        raise
    return manifest
