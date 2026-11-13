#!/usr/bin/env python3
"""Check that pack/check/restore do not buffer the source in memory.

The source is a large sparse-ish file built by streaming writes; the job
runs in a subprocess and its peak RSS is read with
``resource.getrusage(RUSAGE_CHILDREN)``.  Unlike ``stress_demo.py`` this
script also *deletes* a stripe pair and restores, so decode-side memory is
measured too.
"""

import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

WORKER = r'''
import json, os, sys, shutil
sys.path.insert(0, sys.argv[1])
from archive_stripe.check import pack_source, check_pack
from archive_stripe.restore import restore_pack
src, pack, damaged, out, k, B = sys.argv[2:8]
k, B = int(k), int(B)
pack_source(src, pack, k, B)
rep = check_pack(pack)
assert rep["healthy"], rep
os.makedirs(damaged, exist_ok=True)
for name in os.listdir(pack):
    shutil.copyfile(os.path.join(pack, name), os.path.join(damaged, name))
for gone in ("D0.bin", "D1.bin"):
    os.unlink(os.path.join(damaged, gone))
rep2 = check_pack(damaged)
assert rep2["recoverable"], rep2
restore_pack(damaged, out)
print(json.dumps({"mode": rep2["recovery_mode"]}))
'''


def stream_sha(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for piece in iter(lambda: fh.read(chunk), b""):
            h.update(piece)
    return h.hexdigest()


def generate(path, size):
    # Deterministic pattern with all byte values, written block by block.
    seed = bytes((i * 197 + 23) % 256 for i in range(1 << 16))
    with open(path, "wb") as fh:
        left = size
        while left:
            take = min(left, len(seed))
            fh.write(seed[:take])
            left -= take


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--size-mib", type=float, default=1024.0)
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--block", type=int, default=1 << 20)
    parser.add_argument("--budget-mib", type=float, default=96.0)
    parser.add_argument("--work-dir", default="/tmp/archive_memory_probe")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    work = os.path.abspath(args.work_dir)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    src = os.path.join(work, "source.bin")
    pack = os.path.join(work, "pack")
    damaged = os.path.join(work, "damaged")
    out = os.path.join(work, "restored.bin")
    size = int(args.size_mib * (1 << 20))
    generate(src, size)

    worker = os.path.join(work, "worker.py")
    with open(worker, "w", encoding="utf-8") as fh:
        fh.write(WORKER)

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-B", worker, _ROOT, src, pack, damaged, out,
         str(args.k), str(args.block)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(proc.stdout, proc.stderr, file=sys.stderr)
        raise SystemExit(proc.returncode)
    peak_mib = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    info = json.loads(proc.stdout.strip().splitlines()[-1])
    src_sha = stream_sha(src)
    out_sha = stream_sha(out)
    report = {
        "source_bytes": size,
        "dataset_mib": round(size / (1 << 20), 1),
        "k": args.k,
        "block_size": args.block,
        "lost_stripes": ["D0.bin", "D1.bin"],
        "recovery_mode": info["mode"],
        "source_sha256": src_sha,
        "restored_sha256": out_sha,
        "digest_match": src_sha == out_sha,
        "peak_rss_mib": round(peak_mib, 2),
        "budget_mib": args.budget_mib,
        "peak_below_budget": peak_mib <= args.budget_mib,
        "wall_seconds": round(elapsed, 2),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if not report["digest_match"]:
        raise SystemExit("digest mismatch")
    if not report["peak_below_budget"]:
        raise SystemExit("peak RSS above budget")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
