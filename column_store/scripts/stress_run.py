#!/usr/bin/env python3
"""Scalable-input stress run for column_store.

Generates a synthetic supplier event file (streamed to disk, so generation
itself stays O(1) in memory), builds a store with a configurable row group
size, runs a narrow scan, and records:

* configured row group size, input row count and bytes;
* peak RSS of the build process and of the narrow scan process (measured by
  ``os.wait4`` rusage, i.e. the kernel's high-water mark);
* the scan's page-read accounting and actual data bytes read;
* the same facts for a full-column baseline scan, for comparison.

Peak memory of a separate process is the honest measurement: Python does not
release memory to the OS, so an in-process high-water mark would include the
generator.  Each phase runs as its own child.

    python3 -B scripts/stress_run.py --rows 200000 --row-group-rows 4096 \
        --work-dir /tmp/column_store_stress --json /tmp/stress.json
"""

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCHEMA = [
    {"name": "event_id", "type": "integer", "nullable": False},
    {"name": "region", "type": "string", "nullable": True},
    {"name": "amount_cents", "type": "integer", "nullable": True},
    {"name": "is_refund", "type": "boolean", "nullable": False},
    {"name": "description", "type": "string", "nullable": True},
]

GEN = r'''
import json, sys
path, n = sys.argv[1], int(sys.argv[2])
regions = ["Norte", "Sul", "Leste", "Oeste", "Norte", "Norte", "Sul"]
small = max(1, n // 3)
with open(path, "w", encoding="utf-8", newline="\n") as fh:
    for i in range(n):
        if i < small:
            amount = [-(10**6 + i), -1, 0, 9, 10][i % 5]
        else:
            amount = [10, 250000, -1, 2**41 + i, 7, 10**9 + i, 0, 9][i % 8]
        row = {
            "event_id": 7000000 + i,
            "region": None if i % 9 == 0 else regions[i % len(regions)],
            "amount_cents": None if i % 6 == 0 else amount,
            "is_refund": i % 5 == 0,
            "description": None if i % 11 == 0 else (
                "settlement note %08d %s" % (i, ("detail-%d " % i) * 7)),
        }
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        fh.write("\n")
'''

RUN_BUILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from column_store.schema import load_schema
from column_store.builder import build
r = build(sys.argv[2], sys.argv[3], load_schema(sys.argv[4]),
          row_group_max=int(sys.argv[5]))
print(json.dumps({"row_count": r.row_count, "groups": r.group_count}))
'''

RUN_SCAN = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from column_store.scanner import scan_to_file
written, report = scan_to_file(sys.argv[2], sys.argv[3],
                               json.loads(sys.argv[4]),
                               json.loads(sys.argv[5]),
                               report_path=sys.argv[6])
print(json.dumps({"rows": written}))
'''


def run_measured_wait4(argv):
    """Like run_measured but uses wait4 for the child's peak RSS."""
    t0 = time.time()
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()
    pid = os.posix_spawn(argv[0], argv, dict(os.environ,
                                             PYTHONDONTWRITEBYTECODE="1"),
                         file_actions=[
                             (os.POSIX_SPAWN_DUP2, out_w, 1),
                             (os.POSIX_SPAWN_DUP2, err_w, 2),
                         ])
    os.close(out_w)
    os.close(err_w)
    # Read concurrently: capture into files to avoid pipe deadlock.
    import tempfile
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        import select
        fds = {out_r: fo, err_r: fe}
        open_fds = set(fds)
        import fcntl
        for fd in open_fds:
            fcntl.fcntl(fd, fcntl.F_SETFL, os.O_NONBLOCK)
        while open_fds:
            r, _, _ = select.select(list(open_fds), [], [], 1.0)
            for fd in r:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    open_fds.discard(fd)
                    continue
                fds[fd].write(chunk)
        _, status, usage = os.wait4(pid, 0)
        fo.seek(0)
        fe.seek(0)
        out = fo.read().decode()
        err = fe.read().decode()
    os.close(out_r)
    os.close(err_r)
    code = os.waitstatus_to_exitcode(status)
    return code, out, err, time.time() - t0, usage.ru_maxrss


def write_child(path, source):
    with open(path, "w") as fh:
        fh.write(source)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=200000)
    p.add_argument("--row-group-rows", type=int, default=4096)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--json", default=None)
    a = p.parse_args(argv)
    work = os.path.abspath(a.work_dir)
    if os.path.lexists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    src = os.path.join(work, "events.jsonl")
    schema_path = os.path.join(work, "schema.json")
    store = os.path.join(work, "events.store")
    with open(schema_path, "w") as fh:
        json.dump({"columns": SCHEMA}, fh)
    gen = os.path.join(work, "gen.py")
    write_child(gen, GEN)
    r_build = os.path.join(work, "run_build.py")
    write_child(r_build, RUN_BUILD)
    r_scan = os.path.join(work, "run_scan.py")
    write_child(r_scan, RUN_SCAN)

    code, out, err, gen_wall, gen_rss = run_measured_wait4(
        [sys.executable, "-I", gen, src, str(a.rows)])
    if code != 0:
        sys.stderr.write(err)
        return 2
    input_bytes = os.path.getsize(src)

    code, out, err, build_wall, build_rss = run_measured_wait4(
        [sys.executable, "-I", r_build, ROOT, src, store, schema_path,
         str(a.row_group_rows)])
    if code != 0:
        sys.stderr.write(err)
        return 2
    build_info = json.loads(out.strip().splitlines()[-1])
    store_bytes = 0
    for dirpath, _dirs, files in os.walk(store):
        for name in files:
            store_bytes += os.path.getsize(os.path.join(dirpath, name))

    narrow_out = os.path.join(work, "narrow.jsonl")
    narrow_report = os.path.join(work, "narrow.report.json")
    conditions = [["amount_cents", "gt", 10]]
    code, out, err, narrow_wall, narrow_rss = run_measured_wait4(
        [sys.executable, "-I", r_scan, ROOT, store, narrow_out,
         json.dumps(["region"]), json.dumps(conditions), narrow_report])
    if code != 0:
        sys.stderr.write(err)
        return 2
    with open(narrow_report) as fh:
        report = json.load(fh)
    narrow_rows = sum(1 for _ in open(narrow_out, "rb"))
    with open(narrow_out, "rb") as fh:
        first_narrow = fh.readline().decode("utf-8")[:120]
    narrow_size = os.path.getsize(narrow_out)

    full_out = os.path.join(work, "full.jsonl")
    full_report = os.path.join(work, "full.report.json")
    code, out, err, full_wall, full_rss = run_measured_wait4(
        [sys.executable, "-I", r_scan, ROOT, store, full_out,
         json.dumps(["event_id", "region", "amount_cents", "is_refund",
                     "description"]), json.dumps([]), full_report])
    if code != 0:
        sys.stderr.write(err)
        return 2
    with open(full_report) as fh:
        full = json.load(fh)

    result = {
        "rows": a.rows,
        "row_group_rows": a.row_group_rows,
        "input_bytes": input_bytes,
        "store_bytes": store_bytes,
        "groups": build_info["groups"],
        "build": {"wall_s": round(build_wall, 3),
                  "peak_rss_kib": build_rss},
        "narrow_scan": {
            "conditions": conditions,
            "projection": ["region"],
            "wall_s": round(narrow_wall, 3),
            "peak_rss_kib": narrow_rss,
            "rows_out": narrow_rows,
            "output_bytes": narrow_size,
            "first_row": first_narrow,
            "row_groups_total": report["row_groups_total"],
            "row_groups_checked": report["row_groups_checked"],
            "row_groups_skipped_by_stats":
                report["row_groups_skipped_by_stats"],
            "columns_read": report["columns_read"],
            "pages_read": report["pages_read"],
            "data_bytes_read": report["bytes_read_from_data_pages"],
            "index_bytes_read": report["index_bytes_read"],
        },
        "full_scan": {
            "wall_s": round(full_wall, 3),
            "peak_rss_kib": full_rss,
            "pages_read": full["pages_read"],
            "data_bytes_read": full["bytes_read_from_data_pages"],
        },
        "narrow_to_full_data_bytes": (
            round(report["bytes_read_from_data_pages"]
                  / full["bytes_read_from_data_pages"], 4)
            if full["bytes_read_from_data_pages"] else None),
    }
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if a.json:
        if os.path.lexists(a.json):
            print("refusing to overwrite %s" % a.json, file=sys.stderr)
            return 2
        with open(a.json, "w") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
