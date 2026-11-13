"""Arithmetic in GF(256) with the primitive polynomial 0x11d.

Addition and subtraction are both byte-wise XOR.  Multiplication is
carry-less polynomial multiplication reduced modulo ``x^8 + x^4 + x^3 + x^2 + 1``
(0x11d).  The field element 2 (``0x02``) is a generator, so ``c_i = 2^i`` for
``i = 0 .. k-1`` are the distinct coefficients used by the Q parity stripe.

Tables are built once at import time.  Block operations use ``bytes.translate``
and integer XOR so that work stays proportional to the block length and
memory stays at O(block length).  Nothing here stores a whole source file.
"""

_PRIMITIVE = 0x11D
_ORDER = 256

_MUL_TABLE = [bytearray(_ORDER) for _ in range(_ORDER)]
_INV_TABLE = bytearray(_ORDER)
_TRANSLATE = {}
_COEFF_CACHE = {}


def _build_tables():
    for a in range(_ORDER):
        row = _MUL_TABLE[a]
        for b in range(_ORDER):
            row[b] = _mul_slow(a, b)
        _TRANSLATE[a] = bytes(row)
    for a in range(1, _ORDER):
        for b in range(1, _ORDER):
            if _MUL_TABLE[a][b] == 1:
                _INV_TABLE[a] = b
                break
        else:  # pragma: no cover - impossible for an irreducible polynomial
            raise AssertionError(f"no inverse for 0x{a:02x}")


def _mul_slow(a, b):
    """Carry-less multiply of two bytes, reduced by 0x11d."""
    product = 0
    while b:
        if b & 1:
            product ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= _PRIMITIVE
    return product & 0xFF


_build_tables()


def add(a, b):
    """Field addition: byte-wise XOR."""
    return a ^ b


def sub(a, b):
    """Field subtraction: byte-wise XOR (identical to addition)."""
    return a ^ b


def mul(a, b):
    return _MUL_TABLE[a][b]


def inv(a):
    """Multiplicative inverse; ``inv(0)`` is undefined and raises."""
    if a == 0:
        raise ZeroDivisionError("0 has no multiplicative inverse in GF(256)")
    return _INV_TABLE[a]


def div(a, b):
    return _MUL_TABLE[a][_INV_TABLE[b]]


def pow_alpha(exponent):
    """Return ``2^exponent`` in GF(256) for any non-negative exponent."""
    if exponent < 0:
        raise ValueError("negative exponent")
    value = 1
    base = 2
    e = exponent
    while e:
        if e & 1:
            value = _MUL_TABLE[base][value]
        base = _MUL_TABLE[base][base]
        e >>= 1
    return value


def coefficients(k):
    """``[2^0, 2^1, ..., 2^(k-1)]`` (Q coefficients for data stripe i)."""
    if k not in _COEFF_CACHE:
        _COEFF_CACHE[k] = tuple(pow_alpha(i) for i in range(k))
    return _COEFF_CACHE[k]


def _xor_ints(a, b):
    return int.from_bytes(a, "little") ^ int.from_bytes(b, "little")


def scale(buf, factor):
    """Return ``factor * buf`` as a new bytearray (does not modify *buf*)."""
    if factor == 0:
        return bytearray(len(buf))
    if factor == 1:
        return bytearray(buf)
    return bytearray(bytes(buf).translate(_TRANSLATE[factor]))


def add_scaled(dest, src, factor):
    """``dest ^= factor * src`` in place; both must have equal length."""
    if factor == 0 or len(src) == 0:
        return dest
    if len(dest) != len(src):
        raise ValueError("add_scaled operands must have equal length")
    scaled = bytes(src).translate(_TRANSLATE[factor])
    result = _xor_ints(dest, scaled)
    dest[:] = result.to_bytes(len(dest), "little")
    return dest


def xor_into(dest, src):
    """``dest ^= src`` in place; both must have equal length."""
    if len(dest) != len(src):
        raise ValueError("xor_into operands must have equal length")
    if not dest:
        return dest
    result = _xor_ints(dest, src)
    dest[:] = result.to_bytes(len(dest), "little")
    return dest
