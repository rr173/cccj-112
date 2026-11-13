"""Naive nested-loop left join used as the oracle in tests.

This implementation deliberately ignores every external-memory concern and
expresses the join rules in the most direct way possible:

* scan left rows in physical order (1-based row numbers);
* for each left row, scan *all* right rows and keep every matching pair;
* a left row without matches is emitted once with a null right side;
* right-only rows are never emitted;
* sort the final result by (left_row, right_row), null right row last within
  a left row (an unmatched left row sorts after its own matches; it has no
  matches in that case anyway).

It shares no code with the production pipeline, so agreement between the two
is meaningful.  Duplicate input rows remain distinct because row numbers are
physical.
"""

import json

from handover_join.errors import InputDecodeError, KeyError_


def _read_jsonl(path, side):
    rows = []
    with open(path, "rb") as fh:
        for raw in fh:
            if raw.endswith(b"\n"):
                raw = raw[:-1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
            if not raw:
                raise InputDecodeError(side, len(rows) + 1, "empty line")
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InputDecodeError(side, len(rows) + 1, str(exc)) from exc
            if not isinstance(obj, dict):
                raise InputDecodeError(side, len(rows) + 1, "not a JSON object")
            rows.append(obj)
    return rows


def _component(obj, field, type_name, side, row):
    if field not in obj:
        return None, False  # absent -> never matches, not an error
    value = obj[field]
    if value is None:
        return None, False
    if type_name == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise KeyError_(side, row, field, type_name, value)
        return value, True
    if not isinstance(value, str):
        raise KeyError_(side, row, field, type_name, value)
    return value, True


def _key_tuple(obj, components, side, row):
    """Return ``(values, usable)``; usable is False when a key is null/absent."""
    idx = 0 if side == "left" else 1
    values = []
    for component in components:
        value, usable = _component(
            obj, component[idx], component[2], side, row
        )
        if not usable:
            return None, False
        values.append(value)
    return tuple(values), True


def naive_join(left_path, right_path, components):
    """Return the expected output as a list of plain dictionaries, fully sorted."""
    left_rows = _read_jsonl(left_path, "left")
    right_rows = _read_jsonl(right_path, "right")

    right_keys = []
    for rnum, robj in enumerate(right_rows, start=1):
        right_keys.append(_key_tuple(robj, components, "right", rnum))

    output = []
    for lnum, lobj in enumerate(left_rows, start=1):
        lkey, l_usable = _key_tuple(lobj, components, "left", lnum)
        matched = False
        if l_usable:
            for rnum, (rkey, r_usable) in enumerate(right_keys, start=1):
                if r_usable and rkey == lkey:  # component-wise tuple equality
                    output.append(
                        {
                            "left_row": lnum,
                            "right_row": rnum,
                            "left": lobj,
                            "right": right_rows[rnum - 1],
                        }
                    )
                    matched = True
        if not matched:
            output.append(
                {
                    "left_row": lnum,
                    "right_row": None,
                    "left": lobj,
                    "right": None,
                }
            )

    output.sort(
        key=lambda rec: (
            rec["left_row"],
            rec["right_row"] if rec["right_row"] is not None else float("inf"),
        )
    )
    return output


def load_output(path):
    """Parse a production JSONL output into Python dictionaries."""
    result = []
    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                result.append(json.loads(raw))
    return result
