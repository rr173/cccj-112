"""Block-at-a-time encode and decode for archive stripe packs.

Stripe order
------------
The source is cut into blocks of ``B`` bytes.  Stripe step ``s`` takes the
next ``k`` source blocks in order and places them on the k data stripes::

    step s:  D0 <- source block s*k + 0
             D1 <- source block s*k + 1
             ...
             D(k-1) <- source block s*k + (k-1)

and then appends one P block and one Q block for that step.  Each data
stripe file holds one block per step, so every stripe file has the same
length ``ceil(source_length / (k*B)) * B``.  When the source ends in the
middle of a stripe step, the missing blocks of that step are all-zero; this
zero padding exists **for encoding only**.  The manifest records the true
source length and restore truncates back to it.

Source bytes are never decoded as text: every path here treats ``bytes`` /
``bytearray`` opaquely.

Memory is bounded by ``O(k * B)``: one block buffer per stripe for exactly
one stripe step, plus block-sized solve scratch.  Nothing is buffered per
whole file.
"""

import hashlib
import os
import tempfile

from . import gf256
from .errors import InsufficientStripeError, LayoutError
from .manifest import stripe_names

_IO_BUF = 1 << 20


def validate_params(k, block_size):
    if isinstance(k, bool) or not isinstance(k, int):
        raise LayoutError("k must be an integer")
    if not (2 <= k <= 8):
        raise LayoutError(f"k must be in [2, 8], got {k}")
    if isinstance(block_size, bool) or not isinstance(block_size, int):
        raise LayoutError("B must be an integer")
    if block_size < 1:
        raise LayoutError(f"B must be >= 1, got {block_size}")
    if block_size > (1 << 30):
        raise LayoutError(f"B is implausibly large: {block_size}")
    return k, block_size


def stripe_layout(k, source_length, block_size):
    """Return ``(stripe_length, steps)`` for a source of *source_length*.

    ``stripe_length`` is the length of **every** stripe file and ``steps`` is
    the number of stripe steps (rows) in the pack.
    """
    if source_length < 0:
        raise LayoutError("source length must be non-negative")
    if source_length == 0:
        return 0, 0
    per_step = k * block_size
    steps = (source_length + per_step - 1) // per_step
    return steps * block_size, steps


class PackResult:
    __slots__ = ("k", "block_size", "source_length", "stripe_length",
                 "steps", "source_sha256", "stripes")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw[slot])


class _DigestWriter:
    """File object that hashes and counts every byte written through it."""

    __slots__ = ("fh", "sha", "size")

    def __init__(self, fh):
        self.fh = fh
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, data):
        view = memoryview(data)
        self.sha.update(view)
        self.size += len(view)
        self.fh.write(view)


def encode_file(source_path, pack_dir, k, block_size, *, progress=None):
    """Encode *source_path* into *pack_dir* as k data + 2 parity stripes.

    The pack directory must already exist and be empty, so an interrupted
    pack never looks complete.  Stripe files are written first; the manifest
    is written last, atomically.  On any error every stripe file written by
    this call is removed again.
    """
    validate_params(k, block_size)
    source_length = os.path.getsize(source_path)
    stripe_len, steps = stripe_layout(k, source_length, block_size)
    if not os.path.isdir(pack_dir):
        raise LayoutError(f"pack directory does not exist: {pack_dir}")
    if os.listdir(pack_dir):
        raise LayoutError(
            f"pack directory is not empty: {pack_dir} "
            "(use a fresh directory, or pack --force to replace a pack)"
        )

    names = stripe_names(k)
    coeffs = gf256.coefficients(k)
    handles = {}
    writers = {}
    try:
        for name in names:
            fh = open(os.path.join(pack_dir, name), "wb", buffering=_IO_BUF)
            handles[name] = fh
            writers[name] = _DigestWriter(fh)

        source_sha = hashlib.sha256()
        read_total = 0
        with open(source_path, "rb", buffering=_IO_BUF) as src:
            for step in range(steps):
                p = bytearray(block_size)
                q = bytearray(block_size)
                for i in range(k):
                    remaining = source_length - read_total
                    want = min(block_size, remaining) if remaining > 0 else 0
                    block = src.read(want)
                    if len(block) != want:
                        raise LayoutError(
                            f"source ended early while encoding: expected "
                            f"{want} bytes, got {len(block)}"
                        )
                    if want:
                        source_sha.update(block)
                        read_total += want
                    if len(block) < block_size:
                        # Encoding-only zero pad for the final partial step.
                        block = block + b"\x00" * (block_size - len(block))
                    writers[f"D{i}.bin"].write(block)
                    gf256.xor_into(p, block)
                    gf256.add_scaled(q, block, coeffs[i])
                writers["P.bin"].write(p)
                writers["Q.bin"].write(q)
                if progress is not None:
                    progress(step + 1, steps)
            spare = src.read(1)
        if spare:
            raise LayoutError(
                f"source {source_path} grew while it was read; refusing to "
                "produce an inconsistent pack"
            )
        if read_total != source_length:
            raise LayoutError(
                f"source {source_path} length changed while reading: "
                f"expected {source_length}, read {read_total}"
            )
    except BaseException:
        for fh in handles.values():
            try:
                fh.close()
            except OSError:
                pass
        for name in names:
            try:
                os.unlink(os.path.join(pack_dir, name))
            except OSError:
                pass
        raise
    else:
        for fh in handles.values():
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()

    entries = []
    for pos, name in enumerate(names):
        kind = "data" if name.startswith("D") else "parity"
        index = pos if kind == "data" else name[0]
        entries.append(
            {
                "name": name,
                "role": kind,
                "index": index,
                "length": writers[name].size,
                "sha256": writers[name].sha.hexdigest(),
            }
        )
    return PackResult(
        k=k,
        block_size=block_size,
        source_length=source_length,
        stripe_length=stripe_len,
        steps=steps,
        source_sha256=source_sha.hexdigest(),
        stripes=entries,
    )


# ---------------------------------------------------------------------------
# Restore-side reconstruction
# ---------------------------------------------------------------------------


class _Reader:
    """Sequential reader limited to a stripe's declared length."""

    __slots__ = ("fh", "remaining")

    def __init__(self, path, length):
        self.fh = open(path, "rb", buffering=_IO_BUF)
        self.remaining = length

    def read(self, n):
        n = min(n, self.remaining)
        if n == 0:
            return b""
        data = self.fh.read(n)
        if len(data) != n:
            raise LayoutError(f"stripe {self.fh.name} ended early in restore")
        self.remaining -= n
        return data

    def close(self):
        try:
            self.fh.close()
        except OSError:
            pass


def plan_recovery(k, trusted):
    """Decide how to rebuild from the set of trustworthy stripe names.

    Returns ``(missing, mode)`` where *missing* is the sorted list of data
    stripe indices that must be rebuilt and *mode* is one of ``"identity"``,
    ``"single_p"``, ``"single_q"`` or ``"double"``.  Raises
    :class:`InsufficientStripeError` rather than guessing.
    """
    names = stripe_names(k)
    absent = [n for n in names if n not in trusted]
    if len(absent) > 2:
        raise InsufficientStripeError(
            f"{len(absent)} stripes are untrustworthy ({', '.join(absent)}); "
            "double redundancy can rebuild at most two"
        )
    data_missing = [i for i in range(k) if f"D{i}.bin" not in trusted]
    p_ok = "P.bin" in trusted
    q_ok = "Q.bin" in trusted
    if not data_missing:
        return [], "identity"
    if len(data_missing) == 1:
        if p_ok:
            return data_missing, "single_p"
        if q_ok:
            return data_missing, "single_q"
        raise InsufficientStripeError(
            "a data stripe is missing and neither parity stripe is trustworthy"
        )
    if len(data_missing) == 2:
        if p_ok and q_ok:
            return data_missing, "double"
        raise InsufficientStripeError(
            "two data stripes are missing but both parity stripes are not "
            "trustworthy"
        )
    raise InsufficientStripeError(
        f"{len(data_missing)} data stripes are missing; rebuild is limited to two"
    )


def _solve_matrix(coeffs, a, b):
    """Precompute the GF(256) 2x2 solve for unknowns D_a and D_b.

    With u = P + Σ_{known} D_i and v = Q + Σ_{known} c_i D_i::

        [1   1  ] [D_a]   [u]
        [c_a c_b] [D_b] = [v]

    so ``D_a = (c_b*u + v) / (c_a + c_b)`` and
    ``D_b = (c_a*u + v) / (c_a + c_b)``.
    """
    ca = coeffs[a]
    cb = coeffs[b]
    det = ca ^ cb  # GF addition is XOR
    if det == 0:  # pragma: no cover - distinct powers of a generator
        raise LayoutError("degenerate parity matrix")
    return gf256.inv(det), ca, cb


def reconstruct(source_length, stripe_length, block_size, k, readers, trusted):
    """Yield the reconstructed source bytes, one stripe step at a time.

    ``readers`` maps every trustworthy stripe name to an open
    :class:`_Reader`.  At most one block buffer per stripe is alive for one
    step.  Yields exactly ``stripe_length`` bytes in total.
    """
    if stripe_length % block_size:
        raise LayoutError("stripe length is not a multiple of the block size")
    missing, mode = plan_recovery(k, trusted)
    coeffs = gf256.coefficients(k)
    p_ok = "P.bin" in trusted
    q_ok = "Q.bin" in trusted
    double = _solve_matrix(coeffs, missing[0], missing[1]) if mode == "double" else None
    steps = stripe_length // block_size
    if steps * k * block_size < source_length:
        raise LayoutError("stripe length cannot hold the declared source length")

    for _step in range(steps):
        blocks = {}
        if mode == "identity":
            for i in range(k):
                blocks[i] = readers[f"D{i}.bin"].read(block_size)
        else:
            for name in trusted:
                if name.startswith("D"):
                    idx = int(name[1:].split(".")[0])
                    blocks[idx] = readers[name].read(block_size)
            p = readers["P.bin"].read(block_size) if p_ok else None
            q = readers["Q.bin"].read(block_size) if q_ok else None

            if mode in ("single_p", "single_q"):
                a = missing[0]
                if mode == "single_p":
                    acc = bytearray(p)
                    for i in range(k):
                        if i != a:
                            gf256.xor_into(acc, blocks[i])
                    blocks[a] = bytes(acc)
                else:
                    acc = bytearray(q)
                    for i in range(k):
                        if i != a:
                            gf256.add_scaled(acc, blocks[i], coeffs[i])
                    blocks[a] = bytes(gf256.scale(acc, gf256.inv(coeffs[a])))
            else:
                a, b = missing
                det_inv, ca, cb = double
                u = bytearray(p)
                v = bytearray(q)
                for i in range(k):
                    if i == a or i == b:
                        continue
                    gf256.xor_into(u, blocks[i])
                    gf256.add_scaled(v, blocks[i], coeffs[i])
                x = gf256.scale(u, cb)
                gf256.xor_into(x, v)
                x = gf256.scale(x, det_inv)
                y = gf256.scale(u, ca)
                gf256.xor_into(y, v)
                y = gf256.scale(y, det_inv)
                blocks[a] = bytes(x)
                blocks[b] = bytes(y)

        for i in range(k):
            yield blocks[i]


def restore_to_file(pack_dir, manifest, out_path, trusted, *, progress=None):
    """Restore into *out_path* atomically, truncating to the true length.

    The whole reconstruction is written to a temporary file in the target
    directory, its SHA-256 checked against the manifest, and only then
    renamed over *out_path*.  On any failure a pre-existing target is left
    exactly as it was.
    """
    if manifest.source_length < 0:
        raise LayoutError("negative source length")
    plan_recovery(manifest.k, trusted)  # fail before creating any temp file
    if len(trusted) < manifest.k:
        raise InsufficientStripeError(
            f"only {len(trusted)} trustworthy stripes; k={manifest.k} required"
        )

    readers = {}
    tmp_path = None
    try:
        for name in sorted(trusted):
            readers[name] = _Reader(
                os.path.join(pack_dir, name), manifest.stripe_length
            )
        out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
        if not os.path.isdir(out_dir):
            raise LayoutError(f"output directory does not exist: {out_dir}")
        fd, tmp_path = tempfile.mkstemp(prefix=".restore.", dir=out_dir)
        sha = hashlib.sha256()
        written = 0
        with os.fdopen(fd, "wb", buffering=_IO_BUF) as out:
            for chunk in reconstruct(
                manifest.source_length,
                manifest.stripe_length,
                manifest.block_size,
                manifest.k,
                readers,
                trusted,
            ):
                if written >= manifest.source_length:
                    break
                take = min(len(chunk), manifest.source_length - written)
                piece = chunk[:take]
                out.write(piece)
                sha.update(piece)
                written += take
                if progress is not None:
                    progress(written, manifest.source_length)
            if written != manifest.source_length:
                raise LayoutError(
                    f"reconstructed {written} bytes, manifest declares "
                    f"{manifest.source_length}"
                )
            out.flush()
            os.fsync(out.fileno())
        if sha.hexdigest() != manifest.source_sha256:
            raise InsufficientStripeError(
                "reconstruction does not match the manifest source SHA-256; "
                "refusing to publish a possibly corrupt file"
            )
        # A trusted stripe can still be internally wrong if its bytes were
        # altered together with the manifest; the digest above is the final
        # guard.  Padding-only bytes past the true length are never emitted.
        os.replace(tmp_path, out_path)
        tmp_path = None
        return written
    finally:
        for reader in readers.values():
            reader.close()
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
