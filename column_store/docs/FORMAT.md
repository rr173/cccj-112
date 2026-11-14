# column_store — on-disk format, version 1

This document is normative.  It is written so that a second implementation
can read a store without consulting the code in this repository.

All integers in headers, pages and the catalog are **big-endian**.  CRC-32 is
the IEEE 802.3 polynomial as implemented by Python's `zlib.crc32` and by the
common `crc32()` of zlib.  SHA-256 is the usual FIPS 180-4 digest.

## 1. Directory layout

A store is a directory::

    <store>/
      HEADER               root header followed by the catalog
      data/
        g000000.bin       one file per row group, groups in input order
        g000001.bin
        ...

File names are fixed: `HEADER`, the `data` directory, and `data/g%06d.bin`
for group ordinal `i` (zero based, at least six digits, no extra leading
zeroes; the file for ordinal 1234567 is `g1234567.bin`).  **Column names never appear
in a path.**  A reader must reject a catalog that names any other file.

A store is immutable after publication.  The builder writes into a sibling
temporary directory and renames it into place, so a store directory is either
absent or complete.

## 2. Root header (80 bytes at offset 0)

| offset | size | field |
| --- | --- | --- |
| 0 | 8 | magic `b"CSTORE01"` |
| 8 | 4 | format version, `1` |
| 12 | 4 | flags, must be `0` |
| 16 | 8 | catalog offset, must be `80` |
| 24 | 8 | catalog length in bytes, `1..268435456` |
| 32 | 4 | CRC-32 of the catalog bytes |
| 36 | 32 | SHA-256 of the catalog bytes |
| 68 | 8 | reserved, must be `0` |
| 76 | 4 | CRC-32 of header bytes `0..75` |

The file length must be exactly `80 + catalog length`.  A reader verifies the
header CRC before trusting any field, then the catalog CRC and digest before
parsing the catalog.  Any mismatch is fatal for every query: **a damaged
index is always rejected**, whether or not the query would have touched the
damaged part.  A different `format version` is rejected as
unsupported (not as corruption).

## 3. Catalog (index)

Canonical UTF-8 JSON (no BOM), one object::

```json
{
  "format": "column-store",
  "format_version": 1,
  "row_group_max": 4096,
  "row_count": 12345,
  "group_count": 4,
  "schema": [
    {"name": "event time", "type": "integer", "nullable": false},
    {"name": "site, region", "type": "string", "nullable": true},
    {"name": "is_late", "type": "boolean", "nullable": false}
  ],
  "groups": [
    {
      "ordinal": 0,
      "rows": 4096,
      "file": "g000000.bin",
      "file_bytes": 812345,
      "columns": [
        {"encoding": "int64-plain", "offset": 0, "length": 32780,
         "crc32": 123456789, "null_count": 0, "all_null": false,
         "min": 1, "max": 1700000000},
        {"encoding": "str-dict", "offset": 32780, "length": 40010,
         "crc32": 987654321, "null_count": 12, "all_null": false,
         "min": "Aarhus", "max": "Zürich"},
        {"encoding": "bool-bitmap", "offset": 72790, "length": 544,
         "crc32": 24680, "null_count": 0, "all_null": false,
         "min": null, "max": null}
      ]
    }
  ],
  "column_totals": [
    {"name": "event time", "type": "integer", "null_count": 0,
     "all_null": false, "min": 1, "max": 1700000000},
    {"name": "site, region", "type": "string", "null_count": 12,
     "all_null": false, "min": "Aarhus", "max": "Zürich"},
    {"name": "is_late", "type": "boolean", "null_count": 0,
     "all_null": false, "min": null, "max": null}
  ]
}
```

Rules a reader must enforce:

* Unknown or missing keys anywhere in the catalog are a format error.  Names
  are matched exactly; schema order is declaration order.
* `group_count` equals the length of `groups`; ordinals are `0..n-1` in
  order; every group has `1..row_group_max` rows; group rows sum to
  `row_count`.  The sum is the source row count: an empty source produces
  `group_count = 0`, `row_count = 0` and no `data/` files.
* Each group lists exactly one entry per schema column, in schema order.
  `offset` is the byte offset of the page within the group file and pages are
  laid out contiguously starting at 0 with no gaps; the last page ends at
  `file_bytes`.
* `encoding` is valid for the column type (section 5).
* `crc32` is the CRC-32 of the whole page, header included.
* `null_count` is in `0..rows`; `all_null` is true exactly when
  `null_count == rows`.  A non-nullable column has `null_count == 0`.
  `all_null` (with `null_count == rows`) is how an all-null group is
  represented.
* For integer and string columns with at least one non-null value, `min` and
  `max` are the smallest and largest non-null value in the group, compared
  in the type's native order (64-bit signed numeric for integers, Unicode
  code point order for strings).  For an all-null group **and** for boolean
  columns, `min` and `max` are `null`.  `min <= max`.
* `column_totals` repeats the same facts over the whole store; its
  `null_count` equals the sum over groups and its `min`/`max` are the
  extremes over groups.

Statistics are advisory for *skipping*, but readers may not trust them
blindly: a page's decoded null count must equal the catalog `null_count`.

## 4. Data pages

Every column of every group is one page.  Pages are independent; a query
opens and reads only the pages it needs.  A page is:

| offset | size | field |
| --- | --- | --- |
| 0 | 4 | magic `b"CPG1"` |
| 4 | 1 | page kind: `1` integer, `2` boolean, `3` string |
| 5 | 1 | page format version, `1` |
| 6 | 1 | flags: bit 0 `ALL_NULL`, bit 1 `HAS_NULL` |
| 7 | 1 | reserved, must be `0` |
| 8 | 4 | row count (must equal the group's `rows`) |
| 12 | 4 | null bitmap length, `ceil(row_count/8)` |
| 16 | 8 | body length in bytes |
| 24 | 4 | CRC-32 of `null_bitmap || body` |
| 28 | 4 | CRC-32 of header bytes `0..27` |
| 32 | ceil(rows/8) | null bitmap |
| … | body_len | encoded values, non-null rows only, in row order |

Null bitmap: one bit per row, least significant bit first; a set bit means
**null**.  Bits after the last row in the final byte must be zero.  The bitmap
is always present so every page states its own nulls; one column being null
never shifts another column's rows.

`ALL_NULL`/`HAS_NULL` must agree with the bitmap.  An all-null page has
`null_count == row_count` and a body of zero bytes.

The page's CRC (catalog `crc32`) covers all bytes of the page.  A reader
performs an exact read of `length` bytes at `offset` and rejects a short read,
a CRC mismatch, a bad header CRC, a row count differing from the catalog, a
null bitmap length differing from `ceil(rows/8)`, a mismatch between page and
catalog `null_count`, or any length inconsistency.  These checks are
per-page: a damaged page that no query touches does not affect queries that
do not read it, and the store must not be rejected merely because a later
page in a file would be bad.

## 5. Encodings

The encoding name is in the catalog; the body does not repeat it (its layout
is validated against the named encoding).  Values appear in row order with
nulls omitted.

### 5.1 `int64-plain` (integer)

Each non-null value is 8 bytes, two's complement, big-endian.  Body length is
`8 * non_null_count`.

### 5.2 `bool-bitmap` (boolean)

Non-null values packed one bit each, LSB first; a set bit is `true`.  Body
length is `ceil(non_null_count/8)`; padding bits are ignored by readers.

### 5.3 `str-dict` (string)

```
u8   index_width        1, 2 or 4
u32  dictionary_count
repeat dictionary_count times:
    u32  byte_length
    byte[byte_length]    UTF-8, must be well formed
u8/u16/u32[non_null_count]  dictionary index for each non-null row, in order
```

Indices must be `< dictionary_count`; any out-of-range index is a
corruption.  Index width is 1 when the dictionary has `<= 256` entries, 2 for
`<= 65536`, else 4.

### 5.4 `str-dict-rle` (string)

Same header and dictionary as `str-dict`, then:

```
u32  run_count
repeat run_count times:
    index_width  dictionary index
    u32          run length (>= 1)
```

Runs cover the non-null rows exactly, in order.  Indices must be in range and
the run lengths must sum to `non_null_count`.  This is a genuine run-length
compression of the index stream, not a header-only claim: the body contains
only the dictionary and the runs.

### 5.5 `str-prefix` (string)

For every non-null row in order:

```
u32  byte_length
byte[byte_length]   UTF-8, must be well formed
```

### 5.6 Encoding choice at build time

The builder encodes each string group three ways and picks the **smallest
body**.  Equal lengths are resolved by this fixed precedence:
`str-dict` < `str-dict-rle` < `str-prefix`.  The chosen name and the exact
body bytes are recorded; a reader must not re-derive the choice.

Strings are stored and compared as their original Unicode code points.
Nothing is normalized, trimmed, lower-cased, zero-stripped or reinterpreted;
the empty string, strings containing U+0000 and non-ASCII text are all
preserved.  String ordering is code point order — never dictionary index
order and never UTF-8 byte order for comparison purposes (they coincide for
valid UTF-8 for ordering of code points, but a reader must compare the
decoded strings).

## 6. Query semantics for a reader

A scan takes an ordered projection and zero or more predicates combined with
AND.  Supported predicates: `eq`, `lt`, `le`, `gt`, `ge` on integer and
string columns, and `is_null`, `is_not_null` on any column.  Value
comparisons are not defined for boolean columns; a null literal is not a
value comparison (use `is_null`).

For each group, in order:

1. Apply the statistics test.  If some predicate is provably unsatisfiable
   for the whole group, skip the group without reading any of its pages.
   Safe rules using only `null_count`, `min`, `max`:
   * `is_null`: skip if `null_count == 0`;
   * `is_not_null`: skip if `null_count == rows`;
   * any value comparison: skip if `null_count == rows`, or if
     `eq` and the literal lies outside `[min, max]`, or if `lt/le` and
     `min` already fails, or if `gt/ge` and `max` already fails.
   A group that is not skipped is "checked".
2. For a checked group, read and decode the union of predicate columns and
   projection columns.  No other page may be read (a reader that records
   actual I/O must show this).
3. Evaluate predicates row by row; a null value never satisfies a value
   comparison.  Output the projection for surviving rows, in group order and
   row order.

Results are in source row order and projection order, even if a predicate
column is not projected.  An empty result is success.  Besides the
statistics test, a reader may decode a checked group's pages and verify that
each page's decoded null count and (for integer and string columns)
min/max equal the catalog's values; a disagreement is corruption.  Such a
check may only be applied to pages that the query reads anyway.  Unknown columns,
literals whose type does not match the column, and unsupported format
versions are rejected before any output is produced.  Output files are
written only after all needed pages have been read and validated, so a
failure never leaves a partial result.
