"""Independent reference codec for archive packs (test oracle only).

Written from the format description without importing production GF(256),
codec, or restore code.  It parses the manifest with plain ``json``, reads
whole stripe files, and recovers with its own carry-less multiply, so a bug
shared with the production path cannot hide.  It is intentionally simple
(in-memory, per-byte Python) and is only used on small fixtures.

Round-trip model (independent of the production encoder):

* ``reference_pack(data, k, B)`` splits the source into B-byte blocks and
  lays out stripe step ``s`` as source blocks ``s*k .. s*k+k-1`` on D0..D{k-1},
  zero-padding the final step, then P and Q for that step.
* ``reference_restore(pack_dir, k, B, stripe_length, source_length, present)``
  rebuilds the source from a subset of stripe files with a 2x2 solve when
  needed; it never uses the production decoder.
"""

import hashlib
import json
import os

PRIMITIVE = 0x11D


def clmul(a, b):
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= PRIMITIVE
    return product & 0xFF


def gf_inv(a):
    if a == 0:
        raise ZeroDivisionError
    for b in range(1, 256):
        if clmul(a, b) == 1:
            return b
    raise AssertionError("field is not a field")


def coeffs(k):
    out = [1]
    for _ in range(1, k):
        out.append(clmul(out[-1], 2))
    return out


def reference_pack(data, k, block_size):
    """Return ``{name: bytes}`` for a source *data* (hand-laid-out)."""
    per_step = k * block_size
    steps = 0 if not data else (len(data) + per_step - 1) // per_step
    stripe_len = steps * block_size
    stripes = {f"D{i}.bin": bytearray() for i in range(k)}
    stripes["P.bin"] = bytearray()
    stripes["Q.bin"] = bytearray()
    c = coeffs(k)
    padded = data + b"\x00" * (steps * per_step - len(data))
    for s in range(steps):
        blocks = []
        for i in range(k):
            off = (s * k + i) * block_size
            block = padded[off:off + block_size]
            blocks.append(block)
            stripes[f"D{i}.bin"].extend(block)
        p = bytearray(block_size)
        q = bytearray(block_size)
        for i, block in enumerate(blocks):
            for j, byte in enumerate(block):
                p[j] ^= byte
                q[j] ^= clmul(c[i], byte)
        stripes["P.bin"].extend(p)
        stripes["Q.bin"].extend(q)
    return {name: bytes(buf) for name, buf in stripes.items()}, stripe_len


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _cscale(a, block):
    return bytes(clmul(a, x) for x in block)


def reference_restore(stripes, k, block_size, stripe_length, source_length,
                      present):
    """Rebuild source bytes from *stripes* restricted to *present* names."""
    present = set(present)
    c = coeffs(k)
    data_missing = [i for i in range(k) if f"D{i}.bin" not in present]
    p_ok = "P.bin" in present
    q_ok = "Q.bin" in present
    out = bytearray()
    steps = stripe_length // block_size if block_size else 0
    for s in range(steps):
        b = {}
        for name in present:
            b[name] = stripes[name][s * block_size:(s + 1) * block_size]
        if len(data_missing) == 1:
            a = data_missing[0]
            if p_ok:
                acc = bytearray(b["P.bin"])
                for i in range(k):
                    if i != a:
                        acc = bytearray(_xor(acc, b[f"D{i}.bin"]))
                b[f"D{a}.bin"] = bytes(acc)
            elif q_ok:
                acc = bytearray(b["Q.bin"])
                for i in range(k):
                    if i != a:
                        acc = bytearray(
                            _xor(acc, _cscale(c[i], b[f"D{i}.bin"]))
                        )
                b[f"D{a}.bin"] = _cscale(gf_inv(c[a]), bytes(acc))
            else:
                raise ValueError("one data stripe missing, no parity")
        elif len(data_missing) == 2:
            if not (p_ok and q_ok):
                raise ValueError("two data stripes missing, need P and Q")
            a, bb = data_missing
            u = bytearray(b["P.bin"])
            v = bytearray(b["Q.bin"])
            for i in range(k):
                if i in (a, bb):
                    continue
                u = bytearray(_xor(u, b[f"D{i}.bin"]))
                v = bytearray(_xor(v, _cscale(c[i], b[f"D{i}.bin"])))
            det = c[a] ^ c[bb]
            dinv = gf_inv(det)
            b[f"D{a}.bin"] = _cscale(
                dinv, _xor(_cscale(c[bb], bytes(u)), bytes(v))
            )
            b[f"D{bb}.bin"] = _cscale(
                dinv, _xor(_cscale(c[a], bytes(u)), bytes(v))
            )
        elif data_missing:
            raise ValueError(f"{len(data_missing)} data stripes missing")
        for i in range(k):
            out.extend(b[f"D{i}.bin"])
    return bytes(out[:source_length])


def read_pack(pack_dir):
    with open(os.path.join(pack_dir, "manifest.json"), "rb") as fh:
        manifest = json.loads(fh.read().decode("ascii"))
    stripes = {}
    for entry in manifest["stripes"]:
        path = os.path.join(pack_dir, entry["name"])
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            stripes[entry["name"]] = fh.read()
    return manifest, stripes


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
