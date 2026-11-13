#!/usr/bin/env python3
"""Stress test: pack/check/restore a file far larger than the memory budget.

The job runs in a subprocess so its peak RSS can be measured independently
with ``resource.getrusage(RUSAGE_CHILDREN)``.  The source and restored files
are compared by streaming SHA-256, and the report records peak RSS.

Ordinary regression never generates large files; this script is opt-in::

    python3 scripts/stress_demo.py --size-mib 256 --k 6 --block 65536 \
        --work-dir /tmp/archive_stress --report /tmp/archive_stress/report.json

The default memory budget is 64 MiB.  The source is sized well beyond it
(default 256 MiB), while k and B stay small, so a codec that buffered the
whole file would blow the budget.
"""

import argparse
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_WORKER = r'''
import os, sys, json
sys.path.insert(0, sys.argv[1])
from archive_stripe.check import pack_source, check_pack
from archive_stripe.restore import restore_pack
src, pack, out, k, B = sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]), int(sys.argv[6])
pack_source(src, pack, k, B)
report = check_pack(pack)
assert report["healthy"], report
restore_pack(pack, out)
print(json.dumps({"stripe_length": report["stripe_length"],
                  "stripes": len(report["stripes"])}))
'''


def sha256_stream(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_stream_unused(path):
    """Same as above but with a different read size; used as a cross-check."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(3 * 1024 * 1024 + 7), b""):
            h.update(chunk)
    return h.hexdigest()


def make_source(path, size_bytes, seed=0x5EED):
    """Stream a deterministic, non-trivial binary pattern to *path*."""
    block = bytes((i * 131 + 17) % 256 for i in range(1 << 16))
    written = 0
    with open(path, "wb") as fh:
        while written < size_bytes:
            take = min(len(block), size_bytes - written)
            fh.write(block[:take])
            written += take
    return size_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=float, default=256.0)
    parser.add_argument("--k", type=int, default=6)
    parser.add_argument("--block", type=int, default=65536)
    parser.add_argument("--rss-limit-mib", type=float, default=64.0)
    parser.add_argument("--work-dir", default="/tmp/archive_stress")
    parser.add_argument("--report", default=None)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--reuse", action="store_true",
                        help="reuse an existing source file of the right size")
    args = parser.parse_args()

    work = os.path.abspath(args.work_dir)
    if os.path.exists(work) and not args.reuse:
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)
    source = os.path.join(work, "source.bin")
    pack = os.path.join(work, "pack")
    out = os.path.join(work, "restored.bin")
    for path in (pack, out):
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)

    size = int(args.size_mib * (1 << 20))
    if not (args.reuse and os.path.exists(source)
            and os.path.getsize(source) == size):
        t0 = time.time()
        make_source(source, size)
        print(f"generated {size} bytes in {time.time() - t0:.1f}s")

    worker = os.path.join(work, "worker.py")
    with open(worker, "w", encoding="utf-8") as fh:
        fh.write(_WORKER)

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-B", worker, _ROOT, source, pack, out,
         str(args.k), str(args.block)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, file=sys.stderr)
        raise SystemExit(f"worker failed with {proc.returncode}")
    # ru_maxrss on Linux is the sum of the largest RSS this process's
    # *children* have reached.  The streaming SHA-256 check below runs in
    # this process after the measurement, so it cannot inflate this figure.
    peak_kib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    peak_mib = peak_kib / 1024.0

    info = json.loads(proc.stdout.strip().splitlines()[-1])
    src_sha = sha256_stream(source)
    out_sha = sha256_stream_unused(out)
    if src_sha != out_sha:
        raise SystemExit(f"digest mismatch: {src_sha} != {out_sha}")
    if os.path.getsize(source) != os.path.getsize(out):
        raise SystemExit("size mismatch")

    report = {
        "source_bytes": size,
        "source_sha256": src_sha,
        "restored_sha256": out_sha,
        "k": args.k,
        "block_size": args.block,
        "stripe_length": info["stripe_length"],
        "stripes": info["stripes"],
        "peak_rss_mib": round(peak_mib, 2),
        "rss_limit_mib": args.rss_limit_mib,
        "wall_seconds": round(elapsed, 2),
        "within_budget": peak_mib <= args.rss_limit_mib,
        "digest_match": True,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if not report["within_budget"]:
        raise SystemExit(
            f"peak RSS {peak_mib:.1f} MiB exceeds {args.rss_limit_mib} MiB"
        )
    if not args.keep:
        # Keep the report and the pack for inspection; drop the huge copies.
        os.unlink(source)
        os.unlink(out)
        shutil.rmtree(pack, ignore_errors=True)


if __name__ == "__main__":
    main()
