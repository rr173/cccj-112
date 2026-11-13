"""Independent, hand-checkable P/Q example (not production code).

This example deliberately does **not** import the codec or its GF(256)
table.  It re-derives GF(256) multiplication from the primitive polynomial
``0x11d`` with a tiny carry-less multiply loop, and checks a 4-byte source
against values written out by hand below.  The unittest suite also compares
the packer's stripes for the same input with these bytes.

Layout: k=2, B=1.  Source ``00 01 02 03`` gives stripe steps

    step 0:  D0 <- 00   D1 <- 01
    step 1:  D0 <- 02   D1 <- 03

so::

    D0 = 00 02
    D1 = 01 03
    P  = D0 ^ D1
       = (00^01) (02^03) = 01 01

    Q  = c0*D0 ^ c1*D1,  c0 = 2^0 = 1, c1 = 2^1 = 2
       byte 0: 1*00 ^ 2*01 = 00 ^ 02 = 02
       byte 1: 1*02 ^ 2*03 = 02 ^ 06 = 04
    Q  = 02 04

Multiplication used above is plain: 2*1 = 2, 2*3 = 6 (no reduction needed,
both products stay below 0x100).  Recovery checks below exercise the
non-trivial part of the field: the inverse of 2 is ``0x8e`` because
``2 * 0x8e = 0x11c``, and ``0x11c ^ 0x11d = 1``.
"""

PRIMITIVE = 0x11D


def clmul(a, b):
    """Carry-less multiply of two bytes, reduced by 0x11d."""
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= PRIMITIVE
    return product & 0xFF


def main():
    # Hand-computed values (see module docstring).
    hand = {
        "D0": bytes.fromhex("0002"),
        "D1": bytes.fromhex("0103"),
        "P": bytes.fromhex("0101"),
        "Q": bytes.fromhex("0204"),
    }
    c = (1, 2)  # c_i = 2^i

    # Independently recompute D, P, Q from the source and assert equality.
    src = bytes.fromhex("00010203")
    d0 = bytes([src[0], src[2]])
    d1 = bytes([src[1], src[3]])
    p = bytes([d0[i] ^ d1[i] for i in range(2)])
    q = bytes([clmul(c[0], d0[i]) ^ clmul(c[1], d1[i]) for i in range(2)])
    computed = {"D0": d0, "D1": d1, "P": p, "Q": q}
    assert computed == hand, (computed, hand)
    for name in ("D0", "D1", "P", "Q"):
        print(f"{name} = {hand[name].hex()}")

    inv2 = 0x8E
    assert clmul(2, inv2) == 1, "hand inverse of 2 must be 0x8e"

    # Case A: D0 lost, P and D1 survive.  D0 = P ^ D1.
    got = bytes([hand["P"][i] ^ hand["D1"][i] for i in range(2)])
    assert got == hand["D0"], got
    print(f"A: D0 from P,D1       = {got.hex()}")

    # Case B: D1 lost, Q and D0 survive.  D1 = (Q ^ c0*D0) / c1.
    rest = bytes([hand["Q"][i] ^ clmul(c[0], hand["D0"][i]) for i in range(2)])
    got = bytes([clmul(inv2, rest[i]) for i in range(2)])
    assert got == hand["D1"], got
    print(f"B: D1 from Q,D0       = {got.hex()}")

    # Case C: D0 lost, Q and D1 survive (only Q, no P).
    # Q = c0*D0 + c1*D1 with c0 = 1, so D0 = Q ^ 2*D1 directly.
    got = bytes([hand["Q"][i] ^ clmul(c[1], hand["D1"][i]) for i in range(2)])
    assert got == hand["D0"], got
    print(f"C: D0 from Q,D1       = {got.hex()}")

    # Case D: both data stripes lost, both parities survive (2x2 solve).
    #   P = D0 + D1                    -> u = P
    #   Q = c0*D0 + c1*D1 = D0 + 2*D1  -> v = Q
    #   D1 = P + D0  =>  Q = D0 + 2*(P+D0) = 3*D0 + 2*P
    #   D0 = (Q + 2*P) / 3
    inv3 = None
    for x in range(1, 256):
        if clmul(3, x) == 1:
            inv3 = x
            break
    assert inv3 is not None
    two_p = bytes([clmul(2, hand["P"][i]) for i in range(2)])
    numerator = bytes([hand["Q"][i] ^ two_p[i] for i in range(2)])
    d0_solved = bytes([clmul(inv3, b) for b in numerator])
    d1_solved = bytes([hand["P"][i] ^ d0_solved[i] for i in range(2)])
    assert d0_solved == hand["D0"] and d1_solved == hand["D1"]
    print(f"D: D0,D1 from P,Q     = {d0_solved.hex()} {d1_solved.hex()}")
    print(f"   inv(3) = 0x{inv3:02x}   (Q ^ 2P = {numerator.hex()})")


if __name__ == "__main__":
    main()
