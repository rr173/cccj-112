#!/usr/bin/env python3
"""End-to-end demonstration: pack, check, damage, restore — each in a fresh
subprocess so no Python state is shared between the steps.

The demo creates a binary source that is deliberately not valid UTF-8, packs
it with k=4, B=64, then:

1. ``check`` reports HEALTHY.
2. One stripe is truncated and a second is byte-flipped.  ``check`` names
   both precisely; ``restore`` treats both as whole-stripe erasures and
   rebuilds the file, which is compared byte-for-byte with the source.
3. A third stripe is then corrupted too.  ``check`` reports UNRECOVERABLE and
   ``restore`` refuses with a non-zero exit and leaves an existing target
   untouched.
4. A missing manifest is also refused.

Usage::

    python3 scripts/demo.py --work-dir /tmp/archive_demo
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
DEFAULT_WORK = "/tmp/archive_stripe_demo"


def run(args, cwd=None, expect=0):
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "archive_stripe.cli", *args],
        cwd=cwd or _ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    status = "ok" if proc.returncode == expect else f"UNEXPECTED rc={proc.returncode}"
    print(f"$ archive_stripe {' '.join(args)}")
    print(f"  [{status}]")
    for line in (proc.stdout + proc.stderr).rstrip().splitlines():
        print(f"  {line}")
    if proc.returncode != expect:
        raise SystemExit(
            f"command failed: expected exit {expect}, got {proc.returncode}"
        )
    return proc


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=DEFAULT_WORK)
    args = parser.parse_args()

    work = os.path.abspath(args.work_dir)
    if not os.path.isdir(work):
        os.makedirs(work)
    else:
        # Only ever delete artifacts this demo creates; refuse a directory
        # that contains anything else.
        known = {"capture.bin", "pack", "restored.bin", "guarded.bin",
                 "never_written.bin"}
        stray = sorted(set(os.listdir(work)) - known)
        if stray:
            raise SystemExit(
                f"work directory has unrelated files {stray[:5]}; use a "
                "fresh directory"
            )
        for name in known:
            target = os.path.join(work, name)
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.unlink(target)

    source = os.path.join(work, "capture.bin")
    # Non-UTF-8, non-text binary payload including NUL bytes.
    payload = bytes([0x00, 0xFF, 0xFE, 0x80]) * 97 + b"\x89PNG\r\n\x1a\n" + bytes(
        range(256)
    )
    with open(source, "wb") as fh:
        fh.write(payload)
    source_digest = digest(source)
    k, block = 4, 64
    print(f"source: {len(payload)} bytes  sha256={source_digest}  k={k} B={block}")

    pack = os.path.join(work, "pack")
    run(["pack", source, pack, "-k", str(k), "-B", str(block)])
    print()
    run(["check", pack])
    print()

    # Damage 1: truncate one data stripe; damage 2: flip a byte in a parity
    # stripe.  Both are untrustworthy, yet k=4 healthy stripes remain.
    d2 = os.path.join(pack, "D2.bin")
    with open(d2, "r+b") as fh:
        fh.truncate(37)
    p_path = os.path.join(pack, "P.bin")
    with open(p_path, "r+b") as fh:
        fh.seek(5)
        byte = fh.read(1)[0]
        fh.seek(5)
        fh.write(bytes([byte ^ 0x01]))
    print("damaged: truncated D2.bin to 37 bytes; flipped one bit in P.bin")
    print()
    run(["check", pack])
    restored = os.path.join(work, "restored.bin")
    run(["restore", pack, "--output", restored])
    assert digest(restored) == source_digest, "restored bytes differ from source!"
    assert open(restored, "rb").read() == payload
    print(f"  verified: restored sha256={digest(restored)} matches source")
    print()

    # Damage 3: now a third stripe is corrupt.  Restore must refuse and must
    # not overwrite an existing target.
    q_path = os.path.join(pack, "Q.bin")
    with open(q_path, "r+b") as fh:
        fh.seek(0)
        byte = fh.read(1)[0]
        fh.seek(0)
        fh.write(bytes([byte ^ 0x80]))
    print("additionally flipped one bit in Q.bin (3 stripes untrustworthy)")
    print()
    run(["check", pack], expect=2)
    guarded = os.path.join(work, "guarded.bin")
    with open(guarded, "wb") as fh:
        fh.write(b"pre-existing target that must survive")
    before = digest(guarded)
    run(["restore", pack, "--output", guarded], expect=2)
    assert digest(guarded) == before, "failed restore modified an existing target!"
    assert open(guarded, "rb").read() == b"pre-existing target that must survive"
    print("  verified: existing target unchanged after refused restore")
    print()

    # Missing manifest: layout is not guessed and no target is written.
    os.unlink(os.path.join(pack, "manifest.json"))
    fresh = os.path.join(work, "never_written.bin")
    run(["check", pack], expect=2)
    run(["restore", pack, "--output", fresh], expect=2)
    assert not os.path.exists(fresh), "restore created output without a manifest"
    print("  verified: no output file created without a manifest")
    print()
    print(f"demo complete — artifacts kept under {work}")


if __name__ == "__main__":
    main()
