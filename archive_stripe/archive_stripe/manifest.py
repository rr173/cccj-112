"""On-disk layout for a striped archive pack.

A pack directory contains exactly ``k + 2`` stripe files and one manifest::

    <pack>/
      manifest.json          # the only metadata file; small and readable
      D0.bin ... D{k-1}.bin  # data stripes
      P.bin                  # P = sum(D_i)
      Q.bin                  # Q = sum(c_i * D_i)

The manifest records the format version, block length B, stripe locations,
the true source length, and for every stripe its expected on-disk length and
SHA-256.  It contains no source payload bytes, but it is complete enough that
another process can restore the source from the pack directory alone.

Naming is fixed.  The manifest repeats the stripe names so a reader can
verify that a directory really is the pack it claims to be.
"""

import json
import os
import re
import tempfile

from .errors import LayoutError, ManifestError

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"

PARITY_NAMES = ("P.bin", "Q.bin")

K_MIN = 2
K_MAX = 8


def stripe_names(k):
    """Stripe file names in logical order: data D0..D{k-1}, then P, then Q."""
    if not isinstance(k, int) or isinstance(k, bool):
        raise LayoutError("data stripe count k must be an integer")
    if not (K_MIN <= k <= K_MAX):
        raise LayoutError(
            f"data stripe count k must be in [{K_MIN}, {K_MAX}], got {k}"
        )
    return [f"D{i}.bin" for i in range(k)] + list(PARITY_NAMES)


class Manifest:
    """Parsed and validated manifest.  Constructing one never guesses layout."""

    __slots__ = (
        "version",
        "k",
        "block_size",
        "source_length",
        "source_sha256",
        "stripe_length",
        "stripes",
        "raw",
    )

    def __init__(self, *, version, k, block_size, source_length, source_sha256,
                 stripe_length, stripes, raw=None):
        self.version = version
        self.k = k
        self.block_size = block_size
        self.source_length = source_length
        self.source_sha256 = source_sha256
        self.stripe_length = stripe_length
        self.stripes = stripes  # ordered list of dicts, logical D0..Q order
        self.raw = raw

    # -- accessors --------------------------------------------------------

    def stripe_count(self):
        return self.k + 2

    @property
    def steps(self):
        if self.source_length == 0:
            return 0
        per_step = self.k * self.block_size
        return (self.source_length + per_step - 1) // per_step

    def stripe_entry(self, name):
        for entry in self.stripes:
            if entry["name"] == name:
                return entry
        raise ManifestError(f"manifest has no entry for stripe {name!r}")

    def expected_length(self, name):
        return self.stripe_entry(name)["length"]

    def expected_sha256(self, name):
        return self.stripe_entry(name)["sha256"]

    def data_names(self):
        return [f"D{i}.bin" for i in range(self.k)]

    def parity_names(self):
        return list(PARITY_NAMES)

    def all_names(self):
        return self.data_names() + self.parity_names()

    # -- serialisation ----------------------------------------------------

    def to_dict(self):
        return {
            "format": "archive-stripe",
            "version": self.version,
            "k": self.k,
            "block_size": self.block_size,
            "source_length": self.source_length,
            "source_sha256": self.source_sha256,
            "stripe_length": self.stripe_length,
            "stripes": [
                {
                    "name": e["name"],
                    "role": e["role"],
                    "index": e["index"],
                    "length": e["length"],
                    "sha256": e["sha256"],
                }
                for e in self.stripes
            ],
        }

    def to_bytes(self):
        # Stable, human-readable, ASCII-only JSON; no payload bytes here.
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode(
            "ascii"
        )


def _require(obj, key, types, where):
    if key not in obj:
        raise ManifestError(f"manifest is missing field {key!r} ({where})")
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, types):
        raise ManifestError(
            f"manifest field {key!r} has the wrong type ({where}): expected "
            f"{getattr(types, '__name__', 'an integer')}, "
            f"got {type(value).__name__}"
        )
    return value


def _check_sha(value, where):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ManifestError(f"manifest {where} is not a lowercase hex SHA-256")
    return value


def validate_manifest_object(obj):
    """Validate a decoded manifest dict; never infer missing geometry."""
    if not isinstance(obj, dict):
        raise ManifestError("manifest root must be a JSON object")
    if obj.get("format") != "archive-stripe":
        raise ManifestError("manifest 'format' must be 'archive-stripe'")
    version = _require(obj, "version", int, "format version")
    if version != FORMAT_VERSION:
        raise ManifestError(
            f"unsupported manifest format version {version}; this build "
            f"understands version {FORMAT_VERSION}"
        )
    k = _require(obj, "k", int, "data stripe count")
    if not (K_MIN <= k <= K_MAX):
        raise LayoutError(
            f"manifest k={k} is outside the supported range [{K_MIN}, {K_MAX}]"
        )
    block_size = _require(obj, "block_size", int, "block length")
    if block_size < 1 or block_size > (1 << 30):
        raise LayoutError(f"manifest block_size={block_size} is outside [1, 2^30]")
    source_length = _require(obj, "source_length", int, "source length")
    if source_length < 0:
        raise LayoutError(f"manifest source_length={source_length} is negative")
    source_sha256 = _check_sha(obj.get("source_sha256"), "source_sha256")

    stripe_length = _require(obj, "stripe_length", int, "stripe length")
    per_step = k * block_size
    steps = 0 if source_length == 0 else (source_length + per_step - 1) // per_step
    expected_stripe_length = steps * block_size
    if stripe_length != expected_stripe_length:
        raise ManifestError(
            f"manifest stripe_length={stripe_length} contradicts "
            f"ceil({source_length}/{per_step})*{block_size}="
            f"{expected_stripe_length} for k={k}, B={block_size}"
        )

    raw_stripes = obj.get("stripes")
    if not isinstance(raw_stripes, list):
        raise ManifestError("manifest 'stripes' must be an array")
    names = stripe_names(k)
    if len(raw_stripes) != len(names):
        raise ManifestError(
            f"manifest lists {len(raw_stripes)} stripes, expected {len(names)}"
        )
    entries = []
    for name, entry in zip(names, raw_stripes):
        if not isinstance(entry, dict):
            raise ManifestError(f"manifest stripe entry {name!r} is not an object")
        if entry.get("name") != name:
            raise ManifestError(
                f"manifest stripe entry must be {name!r}, "
                f"got {entry.get('name')!r}"
            )
        if name.startswith("D"):
            want_index = int(name[1:].split(".")[0])
            if entry.get("role") != "data" or entry.get("index") != want_index:
                raise ManifestError(
                    f"manifest stripe {name} must have role 'data' and "
                    f"index {want_index}"
                )
        else:
            if entry.get("role") != "parity" or entry.get("index") != name[0]:
                raise ManifestError(
                    f"manifest stripe {name} must have role 'parity' and "
                    f"index {name[0]!r}"
                )
        length = entry.get("length")
        if isinstance(length, bool) or not isinstance(length, int):
            raise ManifestError(f"manifest stripe {name} length is not an integer")
        if length != stripe_length:
            raise ManifestError(
                f"manifest stripe {name} length {length} differs from the "
                f"declared stripe_length {stripe_length}"
            )
        sha = _check_sha(entry.get("sha256"), f"stripe {name} sha256")
        entries.append(
            {
                "name": name,
                "role": entry["role"],
                "index": entry["index"],
                "length": length,
                "sha256": sha,
            }
        )

    return Manifest(
        version=version,
        k=k,
        block_size=block_size,
        source_length=source_length,
        source_sha256=source_sha256,
        stripe_length=stripe_length,
        stripes=entries,
        raw=obj,
    )


def read_manifest(pack_dir):
    """Read and validate ``<pack_dir>/manifest.json``.

    Missing, unreadable, non-JSON, wrong-format, and self-contradictory
    manifests all raise :class:`ManifestError`; nothing is guessed.
    """
    path = os.path.join(pack_dir, MANIFEST_NAME)
    try:
        with open(path, "rb") as fh:
            data = fh.read(4 << 20)  # manifest is tiny; refuse runaway growth
            extra = fh.read(1)
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {path}") from None
    except IsADirectoryError:
        raise ManifestError(f"manifest path is a directory: {path}") from None
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if extra:
        raise ManifestError(f"manifest {path} is implausibly large")
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest {path} is not valid UTF-8 JSON: {exc}") from exc
    return validate_manifest_object(obj)


def write_manifest(pack_dir, manifest):
    """Atomically write the manifest into an existing pack directory."""
    path = os.path.join(pack_dir, MANIFEST_NAME)
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", dir=pack_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(manifest.to_bytes())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
