#!/usr/bin/env python3
"""IO monitor used by tests and the demo (Linux only).

Two independent measurements are offered:

* ``ReadWatcher`` compiles a tiny ``LD_PRELOAD`` shim (``readwatch.c``) and
  records every ``read``/``pread`` the *child process* performs, with the
  return value and the offset.  This is real file I/O, not an estimate.  The
  shim filters on the absolute path of one store data file.

* ``read_segments(path, read_fn)`` wraps a file object and counts the bytes
  a caller reads through it.  The store's scanner uses exact ``os.pread``
  calls, so tests can open a group file, run ``scan()``, and compare
  ``os.stat`` (no reads) with the segment log.

If a C compiler or ``LD_PRELOAD`` is unavailable the ReadWatcher raises
``RuntimeError``; callers should treat that as "cannot prove" rather than
"proved".
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHIM_SOURCE = os.path.join(HERE, "readwatch.c")


def have_cc():
    for c in ("cc", "gcc"):
        try:
            subprocess.run([c, "--version"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True)
            return c
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def build_shim(work_dir):
    cc = have_cc()
    if cc is None:
        raise RuntimeError("no C compiler available for the read watcher")
    so = os.path.join(work_dir, "readwatch.so")
    subprocess.run([cc, "-shared", "-fPIC", "-O2", "-o", so, SHIM_SOURCE,
                    "-ldl", "-lpthread"], check=True)
    return so


def read_watch(so_path, log_path, path):
    """Return environment variables that make a child log reads of *path*."""
    env = dict(os.environ)
    env["LD_PRELOAD"] = so_path
    env["READWATCH_LOG"] = log_path
    env["READWATCH_PATH"] = os.path.abspath(path)
    return env


def parse_log(log_path):
    """Return a list of dicts {op, returned, count, offset} sorted as logged."""
    out = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.rstrip("\n").split(" ", 4)
                if len(parts) < 5:
                    continue
                op, ret, count, off, path = parts
                out.append({"op": op, "returned": int(ret),
                            "count": int(count), "offset": int(off),
                            "path": path})
    except FileNotFoundError:
        return []
    return out


def total_returned(entries):
    return sum(max(0, e["returned"]) for e in entries)


class ReadSegments(object):
    """Wrap a binary file object and record the byte ranges actually read."""

    def __init__(self, fh):
        self.fh = fh
        self.segments = []

    def read(self, n=-1):
        pos = self.fh.tell()
        data = self.fh.read(n)
        self.segments.append((pos, len(data)))
        return data

    def seek(self, pos, whence=0):
        return self.fh.seek(pos, whence)

    def tell(self):
        return self.fh.tell()

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    # Manual helper: python3 io_monitor.py --so X --log Y --path Z -- cmds...
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--so", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    a = p.parse_args()
    env = read_watch(a.so, a.log, a.path)
    r = subprocess.run(a.cmd, env=env)
    ent = parse_log(a.log)
    print(json.dumps({"entries": ent, "bytes": total_returned(ent)},
                     indent=2))
    sys.exit(r.returncode)
