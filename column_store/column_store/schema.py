"""Schema declaration, parsing and validation.

A schema is an ordered tuple of :class:`Column`.  It can be declared in Python
or supplied as a JSON document with ``"columns"`` (or ``"fields"``) in order::

    {"columns": [
        {"name": "event_time", "type": "integer", "nullable": false},
        {"name": "site name",  "type": "string",  "nullable": true},
        {"name": "is_late",    "type": "boolean", "nullable": false}
    ]}

Column names are opaque strings: spaces, punctuation, quotes and non-ASCII
characters are all allowed.  They are never used to build a file path; the
store maps a name to a fixed 64-bit column id and to the tuple position, and
loads encodings from the manifest by index.  A name that is empty, contains
NUL, or occurs twice is rejected.
"""

import json

from .errors import SchemaError

INTEGER = "integer"
STRING = "string"
BOOLEAN = "boolean"
TYPES = (INTEGER, STRING, BOOLEAN)

#: Inclusive bounds of the signed 64-bit integer type.
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

#: Guard against absurd schemas / metadata before allocating.
MAX_COLUMNS = 4096


class Column(object):
    __slots__ = ("name", "type", "nullable")

    def __init__(self, name, type_, nullable):
        self.name = name
        self.type = type_
        self.nullable = nullable

    def __eq__(self, other):
        return (isinstance(other, Column)
                and (self.name, self.type, self.nullable)
                == (other.name, other.type, other.nullable))

    def __hash__(self):
        return hash((self.name, self.type, self.nullable))

    def __repr__(self):
        return "Column(%r, %r, nullable=%r)" % (self.name, self.type,
                                                self.nullable)

    def __reduce__(self):
        return (Column, (self.name, self.type, self.nullable))

    def to_json(self):
        return {"name": self.name, "type": self.type,
                "nullable": self.nullable}


def _check_name(name, where):
    if not isinstance(name, str):
        raise SchemaError("%s: column name must be a string" % where)
    if name == "":
        raise SchemaError("%s: column name must not be empty" % where)
    if "\x00" in name:
        raise SchemaError("%s: column name must not contain NUL" % where)
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaError("%s: column name is not valid Unicode" % where)
    return name


def make_schema(columns):
    """Validate a sequence of Column objects / dicts and return a tuple.

    ``columns`` may be a list of :class:`Column` or of dicts with keys
    ``name``, ``type`` and ``nullable``.  Order is preserved.
    """
    if isinstance(columns, dict):
        # Convenience: {"a": "integer", ...} is accepted only as an ordered
        # mapping; JSON objects preserve order in CPython but a list of
        # declarations is the documented form.
        raise SchemaError("schema must be an ordered list of column "
                          "declarations, not an object")
    parsed = []
    for i, item in enumerate(columns):
        where = "column %d" % i
        if isinstance(item, Column):
            parsed.append(item)
            continue
        if not isinstance(item, dict):
            raise SchemaError("%s: declaration must be an object" % where)
        unknown = set(item) - {"name", "type", "nullable"}
        if unknown:
            raise SchemaError("%s: unknown declaration keys: %s"
                              % (where, ", ".join(sorted(unknown))))
        name = _check_name(item.get("name"), where)
        type_ = item.get("type")
        if type_ not in TYPES:
            raise SchemaError("%s (%r): type must be one of %s"
                              % (where, name, ", ".join(TYPES)))
        if "nullable" not in item:
            raise SchemaError("%s (%r): nullable must be stated"
                              % (where, name))
        nullable = item["nullable"]
        if not isinstance(nullable, bool):
            raise SchemaError("%s (%r): nullable must be true or false"
                              % (where, name))
        parsed.append(Column(name, type_, nullable))
    return freeze_schema(parsed)


def freeze_schema(columns):
    if len(columns) > MAX_COLUMNS:
        raise SchemaError("schema has %d columns; limit is %d"
                          % (len(columns), MAX_COLUMNS))
    if not columns:
        raise SchemaError("schema must declare at least one column")
    seen = {}
    out = []
    for i, col in enumerate(columns):
        if col.type not in TYPES:
            raise SchemaError("column %d: bad type %r" % (i, col.type))
        if not isinstance(col.nullable, bool):
            raise SchemaError("column %d: nullable must be true or false" % i)
        _check_name(col.name, "column %d" % i)
        if col.name in seen:
            raise SchemaError("duplicate column name %r (positions %d and %d)"
                              % (col.name, seen[col.name], i))
        seen[col.name] = i
        out.append(col)
    if not out:
        raise SchemaError("schema must declare at least one column")
    return tuple(out)


def load_schema(path):
    """Read a JSON schema document from *path*."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SchemaError("cannot read schema %s: %s" % (path, exc))
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SchemaError("schema %s is not valid UTF-8 JSON: %s"
                          % (path, exc))
    return schema_from_document(doc, path)


def schema_from_document(doc, source="schema"):
    if not isinstance(doc, dict):
        raise SchemaError("%s: top level must be an object" % source)
    extra = set(doc) - {"columns", "fields"}
    if extra:
        raise SchemaError("%s: unknown top-level keys: %s"
                          % (source, ", ".join(sorted(extra))))
    if ("columns" in doc) == ("fields" in doc):
        raise SchemaError("%s: expected exactly one of 'columns' or 'fields'"
                          % source)
    cols = doc.get("columns", doc.get("fields"))
    if not isinstance(cols, list):
        raise SchemaError("%s: columns must be an array" % source)
    return make_schema(cols)


def schema_to_document(schema):
    return {"columns": [c.to_json() for c in schema]}


def column_index(schema):
    return {c.name: i for i, c in enumerate(schema)}
