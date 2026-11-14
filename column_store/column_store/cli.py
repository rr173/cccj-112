"""Command line entry points: ``build`` and ``scan``.

    python3 -B -m column_store build --schema schema.json \
        --input events.jsonl --store events.store [--row-group-rows N]

    python3 -B -m column_store scan --store events.store \
        --columns region,amount --where 'amount >= 10' \
        --output result.jsonl --report result.report.json

Conditions given with ``--where`` are ANDed.  A condition is either

* ``column OP value`` where OP is ``=``, ``==``, ``<``, ``<=``, ``>``,
  ``>=``, ``is_null`` or ``is_not_null``; the value is read as JSON (so
  ``10`` is an integer, ``"10"`` a string, ``true`` a boolean) and falls back
  to a bare string when it is not valid JSON, or
* a JSON array ``[column, op, value]`` (unambiguous for column names that
  contain operator characters).

Projection names are separated by commas.  Because a column name may itself
contain a comma or spaces, a JSON array of names is also accepted:
``--columns '["site, name", "amount"]'``.

Both commands exit 0 on success and non-zero on failure.  Errors go to
stderr as one line; no result file is left behind by a failed run.
"""

import argparse
import json
import os
import re
import sys

from .builder import (DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STRING_BYTES,
                      DEFAULT_ROW_GROUP_MAX, build)
from .catalog import load_store
from .errors import ColumnStoreError, OutputRefused, UsageError
from .scanner import scan_to_file
from .schema import load_schema

_OPS_SYMBOLIC = ("==", ">=", "<=", ">", "<", "=")
_OPS_WORD = ("is_not_null", "is_null")
_OP_MAP = {"=": "eq", "==": "eq", ">": "gt", ">=": "ge", "<": "lt",
           "<=": "le", "is_null": "is_null",
           "is_not_null": "is_not_null"}
_WORD_OP_RE = re.compile(r"(?:^|\s)(is_null|is_not_null)\s*$")


def parse_where(text):
    text = text.strip()
    if not text:
        raise UsageError("empty --where condition")
    if text.startswith("["):
        try:
            cond = json.loads(text)
        except ValueError as exc:
            raise UsageError("--where %r is not valid JSON: %s" % (text, exc))
        if not isinstance(cond, list) or len(cond) not in (2, 3):
            raise UsageError("--where JSON must be [column, op] or "
                             "[column, op, value]")
        if len(cond) == 2:
            cond = [cond[0], cond[1], None]
        return cond
    m = _WORD_OP_RE.search(text)
    if m:
        name = text[:m.start()].strip()
        if not name:
            raise UsageError("condition %r has no column name" % text)
        return [name, m.group(1), None]
    idx = None
    token = None
    for i, ch in enumerate(text):
        if ch in "<>=":
            for cand in _OPS_SYMBOLIC:
                if text.startswith(cand, i):
                    idx, token = i, cand
                    break
            break
    if idx is None:
        raise UsageError("cannot parse condition %r (expected column op "
                         "value)" % text)
    name = text[:idx].strip()
    if not name:
        raise UsageError("condition %r has no column name" % text)
    rest = text[idx + len(token):].strip()
    if not rest:
        raise UsageError("condition %r has no value" % text)
    try:
        value = json.loads(rest)
    except ValueError:
        value = rest
    return [name, _OP_MAP[token], value]


def parse_columns(values):
    if not values:
        raise UsageError("at least one --columns argument is required")
    names = []
    for text in values:
        stripped = text.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except ValueError as exc:
                raise UsageError("--columns %r is not valid JSON: %s"
                                 % (text, exc))
            if (not isinstance(parsed, list)
                    or not all(isinstance(x, str) for x in parsed)):
                raise UsageError("--columns JSON must be an array of names")
            names.extend(parsed)
            continue
        for part in text.split(","):
            part = part.strip()
            if not part:
                raise UsageError("empty column name in --columns %r" % text)
            names.append(part)
    if not names:
        raise UsageError("--columns must name at least one column")
    return names


def _refuse_existing(path, what):
    if path and os.path.lexists(path):
        raise OutputRefused("refusing to overwrite an existing %s: %s"
                            % (what, path))


def cmd_build(args):
    schema = load_schema(args.schema)
    _refuse_existing(args.store, "store path")
    if args.max_string_bytes > args.max_line_bytes:
        raise UsageError("--max-string-bytes must not exceed --max-line-bytes")
    result = build(args.input, args.store, schema,
                   row_group_max=args.row_group_rows,
                   max_line_bytes=args.max_line_bytes,
                   max_string_bytes=args.max_string_bytes)
    sys.stdout.write(json.dumps(result.to_json(), ensure_ascii=False,
                                sort_keys=True) + "\n")
    return 0


def cmd_scan(args):
    projection = parse_columns(args.columns)
    conditions = [parse_where(text) for text in args.where]
    _refuse_existing(args.output, "output path")
    _refuse_existing(args.report, "report path")
    _, report = scan_to_file(args.store, args.output, projection,
                             conditions, report_path=args.report)
    sys.stdout.write(json.dumps(report.to_json(), ensure_ascii=False,
                                sort_keys=True) + "\n")
    return 0


def cmd_info(args):
    meta = load_store(args.store)
    doc = {
        "store": os.path.abspath(args.store),
        "format_version": 1,
        "row_count": meta.row_count,
        "group_count": len(meta.groups),
        "row_group_max": meta.row_group_max,
        "schema": [c.to_json() for c in meta.schema],
        "groups": [{"ordinal": g.ordinal, "rows": g.rows,
                    "file": g.file, "file_bytes": g.file_bytes,
                    "encodings": [p.encoding for p in g.columns],
                    "null_counts": [p.null_count for p in g.columns],
                    "stats": [[p.min, p.max] for p in g.columns]}
                   for g in meta.groups],
        "column_totals": [t.to_json() for t in meta.column_totals],
    }
    sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="column_store",
                                description="small columnar event store")
    sub = p.add_subparsers(dest="command")

    b = sub.add_parser("build", help="import a UTF-8 JSONL file")
    b.add_argument("--schema", required=True)
    b.add_argument("--input", required=True,
                   help="UTF-8 JSONL source ('-' for stdin)")
    b.add_argument("--store", required=True,
                   help="destination directory (must not exist)")
    b.add_argument("--row-group-rows", type=int,
                   default=DEFAULT_ROW_GROUP_MAX,
                   help="rows per row group (default %d)"
                   % DEFAULT_ROW_GROUP_MAX)
    b.add_argument("--max-line-bytes", type=int,
                   default=DEFAULT_MAX_LINE_BYTES,
                   help="reject a source line longer than this")
    b.add_argument("--max-string-bytes", type=int,
                   default=DEFAULT_MAX_STRING_BYTES,
                   help="reject a decoded string larger than this")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("scan", help="filter and project a store")
    s.add_argument("--store", required=True)
    s.add_argument("--columns", action="append", required=True,
                   help="projection, in output order (repeat or comma "
                        "separate; JSON array for names with commas)")
    s.add_argument("--where", action="append", default=[],
                   help="condition 'col op value' or [col, op, value] "
                        "JSON (repeat for AND)")
    s.add_argument("--output", required=True,
                   help="result JSONL path (must not exist)")
    s.add_argument("--report", default=None,
                   help="side-band statistics JSON path (must not exist)")
    s.set_defaults(func=cmd_scan)

    i = sub.add_parser("info", help="print store metadata")
    i.add_argument("--store", required=True)
    i.set_defaults(func=cmd_info)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return args.func(args)
    except ColumnStoreError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
