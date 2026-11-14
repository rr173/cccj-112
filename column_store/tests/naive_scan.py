#!/usr/bin/env python3
"""Naive row-at-a-time oracle for column_store.

This program deliberately shares **no** decoding or predicate code with the
production package.  It reads the source JSONL file directly (``json.loads``
per line), applies the conditions one row at a time, and prints the projected
fields in the requested order.  It knows nothing about row groups, pages,
dictionaries, statistics or pruning.

    python3 -I naive_scan.py --schema schema.json --input events.jsonl \
        --columns "region,amount" --where "amount>=10" \
        --where "region is_not_null" [--out result.jsonl]

Operator spellings follow the CLI: ``=``, ``==``, ``<``, ``<=``, ``>``, ``>=``,
``is_null``, ``is_not_null``.  ``--where`` may instead be a JSON array
``[column, op, value]``.  Output is JSONL with one JSON array per source row
in source order, exactly like the column store's scan.
"""

import argparse
import json
import sys


def _type_error(where, expected, got):
    raise SystemExit("naive_scan: %s: expected %s, got %s"
                     % (where, expected, got))


def parse_schema_doc(path):
    with open(path, "rb") as f:
        doc = json.loads(f.read().decode("utf-8"))
    cols = doc.get("columns", doc.get("fields"))
    return cols


def parse_condition(text):
    text = text.strip()
    if text.startswith("["):
        c = json.loads(text)
        if len(c) == 2:
            c.append(None)
        if len(c) != 3 or not isinstance(c[0], str) or not isinstance(c[1],
                                                                     str):
            raise SystemExit("naive_scan: bad condition %r" % text)
        return c
    for op in ("is_not_null", "is_null"):
        if text.endswith(op):
            name = text[:-len(op)].strip()
            return [name, op, None]
    for sym in (">=", "<=", "==", ">", "<", "="):
        idx = text.find(sym)
        if idx > 0:
            name = text[:idx].strip()
            rest = text[idx + len(sym):].strip()
            if not rest:
                raise SystemExit("naive_scan: no value in %r" % text)
            try:
                val = json.loads(rest)
            except ValueError:
                val = rest
            return [name, {"=": "eq", "==": "eq", ">": "gt", ">=": "ge",
                           "<": "lt", "<=": "le"}[sym], val]
    raise SystemExit("naive_scan: cannot parse condition %r" % text)


def parse_columns(values):
    """Each --columns argument: a comma list, or a JSON array (names with
    commas need the JSON form)."""
    names = []
    for text in values:
        if text.strip().startswith("["):
            parsed = json.loads(text)
            if (not isinstance(parsed, list)
                    or not all(isinstance(x, str) for x in parsed)):
                raise SystemExit("naive_scan: --columns JSON must be names")
            names.extend(parsed)
        else:
            for part in text.split(","):
                part = part.strip()
                if not part:
                    raise SystemExit("naive_scan: empty column name")
                names.append(part)
    if not names:
        raise SystemExit("naive_scan: no columns")
    return names


def check_literal(col, cond, i):
    name, op, value = cond
    if op in ("is_null", "is_not_null"):
        if value is not None:
            raise SystemExit("naive_scan: condition %d takes no value" % i)
        return
    if col["type"] == "boolean":
        raise SystemExit("naive_scan: condition %d compares a boolean "
                         "column, which is not allowed" % i)
    if value is None:
        raise SystemExit("naive_scan: condition %d has null value" % i)
    if col["type"] == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _type_error("condition %d" % i, "integer", type(value).__name__)

    else:
        if not isinstance(value, str):
            _type_error("condition %d" % i, "string", type(value).__name__)


def matches(row, cond, col):
    name, op, value = cond
    v = row.get(name)
    if op == "is_null":
        return v is None
    if op == "is_not_null":
        return v is not None
    if v is None:
        return False
    if op == "eq":
        return v == value
    if op == "lt":
        return v < value
    if op == "le":
        return v <= value
    if op == "gt":
        return v > value
    if op == "ge":
        return v >= value
    raise SystemExit("naive_scan: bad op %r" % op)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--schema", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--columns", action="append", required=True)
    p.add_argument("--where", action="append", default=[])
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    cols_doc = parse_schema_doc(a.schema)
    by_name = {c["name"]: c for c in cols_doc}
    proj = parse_columns(a.columns)
    for name in proj:
        if name not in by_name:
            raise SystemExit("naive_scan: unknown projection column %r"
                             % name)
    conds = [parse_condition(t) for t in a.where]
    for i, c in enumerate(conds):
        if c[0] not in by_name:
            raise SystemExit("naive_scan: unknown condition column %r"
                             % (c[0],))
        check_literal(by_name[c[0]], c, i)
    out = sys.stdout if a.out is None else open(a.out, "w",
                                                encoding="utf-8",
                                                newline="\n")
    close = a.out is not None
    try:
        with open(a.input, "rb") as f:
            line_no = 0
            for raw in f:
                line_no += 1
                line = raw.rstrip(b"\n")
                if not line:
                    line = b""
                obj = json.loads(line.decode("utf-8"))
                for c in conds:
                    col = by_name[c[0]]
                    if c[0] not in obj:
                        raise SystemExit("naive_scan: line %d missing %r"
                                         % (line_no, c[0]))
                    v = obj[c[0]]
                    if v is not None:
                        if col["type"] == "integer" and (
                                isinstance(v, bool) or not isinstance(v, int)):
                            _type_error("line %d column %r" % (line_no, c[0]),
                                        "integer", type(v).__name__)
                        if col["type"] == "string" and not isinstance(v, str):
                            _type_error("line %d column %r" % (line_no, c[0]),
                                        "string", type(v).__name__)
                    if not matches(obj, c, col):
                        break
                else:
                    out.write(json.dumps([obj[n] for n in proj],
                                         ensure_ascii=False) + "\n")
    finally:
        if close:
            out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
